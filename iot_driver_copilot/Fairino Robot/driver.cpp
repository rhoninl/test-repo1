#include <cstdlib>
#include <cstring>
#include <iostream>
#include <thread>
#include <sstream>
#include <vector>
#include <map>
#include <mutex>
#include <regex>
#include <optional>

// ROS2 headers
#include <rclcpp/rclcpp.hpp>
#include <std_msgs/msg/string.hpp>
#include <fairino_msgs/msg/joint_state.hpp>
#include <fairino_msgs/srv/move.hpp>
#include <fairino_msgs/srv/plan.hpp>
#include <fairino_msgs/srv/trajectory.hpp>
#include <fairino_msgs/srv/task_status.hpp>

// Minimalist HTTP server (no third-party, POSIX sockets)
#include <sys/types.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <unistd.h>

#define BUFFER_SIZE 8192

// ------------------ ENV UTILS ----------------------
std::string get_env(const char* k, const char* def = nullptr) {
    const char* v = getenv(k);
    if (!v) return def ? std::string(def) : std::string();
    return v;
}

// ------------------ ROS2 NODE SINGLETON ----------------------
class Ros2Node {
public:
    static Ros2Node& instance() {
        static Ros2Node instance_;
        return instance_;
    }

    rclcpp::Node::SharedPtr node() { return node_; }

    void init() {
        if (!initialized_) {
            int argc = 0;
            char **argv = nullptr;
            rclcpp::init(argc, argv);
            node_ = rclcpp::Node::make_shared("fairino_http_driver");
            initialized_ = true;
        }
    }

    ~Ros2Node() {
        if (initialized_) {
            rclcpp::shutdown();
        }
    }
private:
    Ros2Node() : initialized_(false) {}
    Ros2Node(const Ros2Node&) = delete;
    Ros2Node& operator=(const Ros2Node&) = delete;

    rclcpp::Node::SharedPtr node_;
    bool initialized_;
};

// ------------------ STATE CACHE ----------------------
struct RobotStatusCache {
    fairino_msgs::msg::JointState last_joint_state;
    std::mutex mtx;
    std::string operational_state = "unknown";
} g_status_cache;

// ------------------ ROS2 SUBSCRIBER THREAD ----------------------
void ros2_status_listener() {
    Ros2Node::instance().init();
    auto node = Ros2Node::instance().node();
    auto sub = node->create_subscription<fairino_msgs::msg::JointState>(
        "/fairino/joint_states", 10,
        [](const fairino_msgs::msg::JointState::SharedPtr msg) {
            std::lock_guard<std::mutex> lk(g_status_cache.mtx);
            g_status_cache.last_joint_state = *msg;
            // For demonstration, assume we store operational_state here as well
            // In reality, you might have another topic for robot state (idle, moving, error)
            g_status_cache.operational_state = msg->header.frame_id;
        }
    );
    rclcpp::spin(node);
}

// ------------------ HTTP UTILS ----------------------
std::string http_response(int code, const std::string& content, const std::string& type="application/json") {
    std::ostringstream oss;
    oss << "HTTP/1.1 " << code << " "
        << (code == 200 ? "OK" : (code == 404 ? "Not Found" : "Error")) << "\r\n"
        << "Content-Type: " << type << "\r\n"
        << "Access-Control-Allow-Origin: *\r\n"
        << "Content-Length: " << content.size() << "\r\n"
        << "Connection: close\r\n"
        << "\r\n"
        << content;
    return oss.str();
}

std::string http_404() {
    return http_response(404, "{\"error\":\"not found\"}");
}

std::string http_bad_request(const std::string& msg) {
    return http_response(400, "{\"error\":\"" + msg + "\"}");
}

// ------------------ JSON ENCODER (MINIMAL) ----------------------
std::string json_joint_state(const fairino_msgs::msg::JointState& js, const std::string& op_state) {
    std::ostringstream oss;
    oss << "{";
    oss << "\"joint_names\":[";
    for (size_t i=0; i<js.name.size(); ++i) {
        if (i) oss << ",";
        oss << "\"" << js.name[i] << "\"";
    }
    oss << "],";
    oss << "\"positions\":[";
    for (size_t i=0; i<js.position.size(); ++i) {
        if (i) oss << ",";
        oss << js.position[i];
    }
    oss << "],";
    oss << "\"velocities\":[";
    for (size_t i=0; i<js.velocity.size(); ++i) {
        if (i) oss << ",";
        oss << js.velocity[i];
    }
    oss << "],";
    oss << "\"efforts\":[";
    for (size_t i=0; i<js.effort.size(); ++i) {
        if (i) oss << ",";
        oss << js.effort[i];
    }
    oss << "],";
    oss << "\"robot_state\":\"" << op_state << "\"";
    oss << "}";
    return oss.str();
}

// ------------------ HTTP ROUTE HANDLERS ----------------------

// POST /robot/trajectory
std::string handle_post_trajectory(const std::string& body) {
    Ros2Node::instance().init();
    auto node = Ros2Node::instance().node();
    auto client = node->create_client<fairino_msgs::srv::Trajectory>("/fairino/execute_trajectory");
    if (!client->wait_for_service(std::chrono::seconds(2)))
        return http_response(503, "{\"error\":\"ROS2 trajectory service unavailable\"}");

    // Expecting: { "trajectory": [ { "positions": [..], "time_from_start": <float> }, ... ] }
    // Minimal JSON parsing (no third party): just look for required fields
    std::vector<std::vector<double>> positions;
    std::vector<double> times;
    std::regex traj_item_re("\\{\\s*\"positions\"\\s*:\\s*\\[([^\\]]+)\\]\\s*,\\s*\"time_from_start\"\\s*:\\s*([0-9.eE+-]+)\\s*\\}");

    auto b = body.c_str();
    auto e = b + body.size();
    std::cmatch m;
    const char* p = b;
    while (std::regex_search(p, e, m, traj_item_re)) {
        // Parse positions
        std::vector<double> joint_pos;
        std::string pos_str = m[1];
        std::stringstream pss(pos_str);
        std::string val;
        while (std::getline(pss, val, ',')) {
            joint_pos.push_back(std::stod(val));
        }
        positions.push_back(joint_pos);
        times.push_back(std::stod(m[2]));
        p = m.suffix().first;
    }
    if (positions.empty())
        return http_bad_request("Invalid or missing trajectory format");

    auto req = std::make_shared<fairino_msgs::srv::Trajectory::Request>();
    for (size_t i = 0; i < positions.size(); ++i) {
        fairino_msgs::msg::JointState traj_point;
        traj_point.position = positions[i];
        // Fake header
        traj_point.header.stamp.sec = (int)times[i];
        traj_point.header.stamp.nanosec = (int)((times[i]-int(times[i]))*1e9);
        req->trajectory.push_back(traj_point);
    }
    auto result = client->async_send_request(req);
    if (rclcpp::spin_until_future_complete(node, result, std::chrono::seconds(5)) != rclcpp::FutureReturnCode::SUCCESS)
        return http_response(500, "{\"error\":\"Failed to execute trajectory\"}");

    return http_response(200, "{\"result\":\"trajectory sent\"}");
}

// POST /robot/move
std::string handle_post_move(const std::string& body) {
    Ros2Node::instance().init();
    auto node = Ros2Node::instance().node();
    auto client = node->create_client<fairino_msgs::srv::Move>("/fairino/move");
    if (!client->wait_for_service(std::chrono::seconds(2)))
        return http_response(503, "{\"error\":\"ROS2 move service unavailable\"}");

    // Expect: { "positions": [..] } OR { "pose": { ... } }
    std::regex positions_re("\"positions\"\\s*:\\s*\\[([^\\]]+)\\]");
    std::cmatch m;
    std::vector<double> positions;
    if (std::regex_search(body.c_str(), m, positions_re)) {
        std::string pos_str = m[1];
        std::stringstream pss(pos_str);
        std::string val;
        while (std::getline(pss, val, ',')) {
            positions.push_back(std::stod(val));
        }
    }
    if (positions.empty())
        return http_bad_request("Missing or invalid positions array");

    auto req = std::make_shared<fairino_msgs::srv::Move::Request>();
    req->target_joint_positions = positions;
    auto result = client->async_send_request(req);
    if (rclcpp::spin_until_future_complete(node, result, std::chrono::seconds(5)) != rclcpp::FutureReturnCode::SUCCESS)
        return http_response(500, "{\"error\":\"Failed to send move command\"}");

    return http_response(200, "{\"result\":\"move command sent\"}");
}

// POST /robot/plan
std::string handle_post_plan(const std::string& body) {
    Ros2Node::instance().init();
    auto node = Ros2Node::instance().node();
    auto client = node->create_client<fairino_msgs::srv::Plan>("/fairino/plan");
    if (!client->wait_for_service(std::chrono::seconds(2)))
        return http_response(503, "{\"error\":\"ROS2 plan service unavailable\"}");

    // Expect: { "goal": { ... } }
    // Only minimal support: look for "positions":[..] in body
    std::regex positions_re("\"positions\"\\s*:\\s*\\[([^\\]]+)\\]");
    std::cmatch m;
    std::vector<double> positions;
    if (std::regex_search(body.c_str(), m, positions_re)) {
        std::string pos_str = m[1];
        std::stringstream pss(pos_str);
        std::string val;
        while (std::getline(pss, val, ',')) {
            positions.push_back(std::stod(val));
        }
    }
    if (positions.empty())
        return http_bad_request("Missing or invalid positions array");

    auto req = std::make_shared<fairino_msgs::srv::Plan::Request>();
    req->goal_joint_positions = positions;
    auto result = client->async_send_request(req);
    if (rclcpp::spin_until_future_complete(node, result, std::chrono::seconds(5)) != rclcpp::FutureReturnCode::SUCCESS)
        return http_response(500, "{\"error\":\"Failed to send plan command\"}");

    auto resp = result.get();
    std::ostringstream oss;
    oss << "{\"task_id\":\"" << resp->task_id << "\"}";
    return http_response(200, oss.str());
}

// GET /robot/status
std::string handle_get_status() {
    std::lock_guard<std::mutex> lk(g_status_cache.mtx);
    return http_response(200, json_joint_state(g_status_cache.last_joint_state, g_status_cache.operational_state));
}

// GET /robot/tasks/{taskId}
std::string handle_get_task_status(const std::string& task_id) {
    Ros2Node::instance().init();
    auto node = Ros2Node::instance().node();
    auto client = node->create_client<fairino_msgs::srv::TaskStatus>("/fairino/task_status");
    if (!client->wait_for_service(std::chrono::seconds(2)))
        return http_response(503, "{\"error\":\"ROS2 task status service unavailable\"}");

    auto req = std::make_shared<fairino_msgs::srv::TaskStatus::Request>();
    req->task_id = task_id;
    auto result = client->async_send_request(req);
    if (rclcpp::spin_until_future_complete(node, result, std::chrono::seconds(5)) != rclcpp::FutureReturnCode::SUCCESS)
        return http_response(500, "{\"error\":\"Failed to get task status\"}");

    auto resp = result.get();
    std::ostringstream oss;
    oss << "{";
    oss << "\"task_id\":\"" << resp->task_id << "\",";
    oss << "\"state\":\"" << resp->state << "\",";
    oss << "\"trajectory\":[";
    for (size_t i=0; i<resp->trajectory.size(); ++i) {
        if (i) oss << ",";
        oss << json_joint_state(resp->trajectory[i], "");
    }
    oss << "]";
    oss << "}";
    return http_response(200, oss.str());
}

// ------------------ HTTP REQUEST ROUTER ----------------------
struct HttpRequest {
    std::string method;
    std::string path;
    std::string body;
};

std::optional<HttpRequest> parse_http_request(const char* buffer, ssize_t len) {
    std::istringstream iss(std::string(buffer, len));
    std::string line;
    HttpRequest req;
    if (!std::getline(iss, line)) return std::nullopt;
    std::istringstream fl(line);
    if (!(fl >> req.method >> req.path)) return std::nullopt;
    std::string headers;
    ssize_t body_start = 0;
    std::string whole(buffer, len);
    auto pos = whole.find("\r\n\r\n");
    if (pos != std::string::npos)
        req.body = whole.substr(pos+4);
    return req;
}

std::string route_http_request(const HttpRequest& req) {
    if (req.method == "POST" && req.path == "/robot/trajectory") {
        return handle_post_trajectory(req.body);
    }
    if (req.method == "POST" && req.path == "/robot/move") {
        return handle_post_move(req.body);
    }
    if (req.method == "POST" && req.path == "/robot/plan") {
        return handle_post_plan(req.body);
    }
    if (req.method == "GET" && req.path == "/robot/status") {
        return handle_get_status();
    }
    std::regex task_re("^/robot/tasks/([a-zA-Z0-9_-]+)$");
    std::smatch m;
    if (req.method == "GET" && std::regex_match(req.path, m, task_re)) {
        return handle_get_task_status(m[1]);
    }
    return http_404();
}

// ------------------ HTTP SERVER ----------------------
void http_server_main(const std::string& host, int port) {
    int server_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (server_fd < 0) { perror("socket"); exit(1); }
    int opt = 1;
    setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    sockaddr_in addr;
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = host == "0.0.0.0" ? INADDR_ANY : inet_addr(host.c_str());
    addr.sin_port = htons(port);
    if (bind(server_fd, (sockaddr*)&addr, sizeof(addr)) < 0) { perror("bind"); exit(1); }
    if (listen(server_fd, 8) < 0) { perror("listen"); exit(1); }

    std::cout << "Fairino HTTP ROS2 driver started on " << host << ":" << port << std::endl;
    while (true) {
        int client_fd = accept(server_fd, nullptr, nullptr);
        if (client_fd < 0) continue;
        std::thread([client_fd]() {
            char buffer[BUFFER_SIZE];
            ssize_t len = recv(client_fd, buffer, sizeof(buffer), 0);
            if (len <= 0) { close(client_fd); return; }
            auto req = parse_http_request(buffer, len);
            std::string reply;
            if (req) {
                reply = route_http_request(*req);
            } else {
                reply = http_bad_request("Malformed HTTP request");
            }
            send(client_fd, reply.c_str(), reply.size(), 0);
            close(client_fd);
        }).detach();
    }
}

// ------------------ MAIN ----------------------
int main() {
    // Read config from env
    std::string server_host = get_env("HTTP_SERVER_HOST", "0.0.0.0");
    int server_port = std::atoi(get_env("HTTP_SERVER_PORT", "8080").c_str());

    // Start ROS2 background listener for /robot/status
    std::thread(ros2_status_listener).detach();

    // Start HTTP server (blocking)
    http_server_main(server_host, server_port);

    return 0;
}