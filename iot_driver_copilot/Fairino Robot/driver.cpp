#include <cstdlib>
#include <cstring>
#include <iostream>
#include <thread>
#include <regex>
#include <sstream>
#include <map>
#include <vector>
#include <chrono>
#include <atomic>
#include <mutex>

#include "rclcpp/rclcpp.hpp"
#include "fairino_msgs/msg/joint_state.hpp"
#include "fairino_msgs/msg/robot_state.hpp"
#include "fairino_msgs/srv/move.hpp"
#include "fairino_msgs/srv/plan.hpp"
#include "fairino_msgs/srv/execute_trajectory.hpp"
#include "fairino_msgs/srv/task_status.hpp"

#ifdef _WIN32
#include <winsock2.h>
#pragma comment(lib, "Ws2_32.lib")
typedef int socklen_t;
#else
#include <sys/socket.h>
#include <netinet/in.h>
#include <unistd.h>
#endif

#define BUFFER_SIZE 8192

// Helper: Environment variable fetch with default
std::string getenv_or_default(const char* var, const char* def) {
    const char* val = std::getenv(var);
    return val ? val : def;
}

// HTTP utilities
std::string http_response(int code, const std::string& status, const std::string& content, const std::string& content_type = "application/json") {
    std::ostringstream oss;
    oss << "HTTP/1.1 " << code << " " << status << "\r\n"
        << "Content-Type: " << content_type << "\r\n"
        << "Access-Control-Allow-Origin: *\r\n"
        << "Content-Length: " << content.length() << "\r\n\r\n"
        << content;
    return oss.str();
}

// Minimal URL decode
std::string url_decode(const std::string& s) {
    std::string res;
    char ch;
    int i, ii;
    for (i = 0; i < s.length(); i++) {
        if (int(s[i]) == 37) {
            sscanf(s.substr(i + 1, 2).c_str(), "%x", &ii);
            ch = static_cast<char>(ii);
            res += ch;
            i = i + 2;
        } else {
            res += s[i];
        }
    }
    return res;
}

// HTTP request parsing
struct HTTPRequest {
    std::string method;
    std::string path;
    std::string body;
    std::map<std::string, std::string> headers;
    std::map<std::string, std::string> query;
};

HTTPRequest parse_http_request(const std::string& req) {
    HTTPRequest r;
    std::istringstream iss(req);
    std::string line;
    std::getline(iss, line);
    std::istringstream lss(line);
    lss >> r.method;
    std::string path_query;
    lss >> path_query;
    size_t qpos = path_query.find('?');
    if (qpos != std::string::npos) {
        r.path = path_query.substr(0, qpos);
        std::string qstr = path_query.substr(qpos + 1);
        std::istringstream qss(qstr);
        std::string token;
        while (std::getline(qss, token, '&')) {
            auto eq = token.find('=');
            if (eq != std::string::npos)
                r.query[url_decode(token.substr(0, eq))] = url_decode(token.substr(eq + 1));
        }
    } else {
        r.path = path_query;
    }
    // headers
    while (std::getline(iss, line) && line != "\r") {
        auto colon = line.find(':');
        if (colon != std::string::npos) {
            std::string key = line.substr(0, colon);
            std::string val = line.substr(colon + 1);
            while (!val.empty() && (val[0] == ' ' || val[0] == '\t')) val.erase(0, 1);
            val.erase(val.find_last_not_of("\r\n") + 1);
            r.headers[key] = val;
        }
    }
    // body
    std::string data;
    while (std::getline(iss, line)) data += line + "\n";
    r.body = data;
    return r;
}

// Threaded HTTP server
class HTTPServer {
public:
    HTTPServer(const std::string& host, int port)
        : host_(host), port_(port), stop_flag_(false) {}
    ~HTTPServer() { stop(); }

    void start(std::function<std::string(const HTTPRequest&)> handler) {
        handler_ = handler;
        stop_flag_ = false;
        server_thread_ = std::thread([this]() { this->run(); });
    }

    void stop() {
        stop_flag_ = true;
#ifdef _WIN32
        closesocket(server_sock_);
        WSACleanup();
#else
        close(server_sock_);
#endif
        if (server_thread_.joinable()) server_thread_.join();
    }

private:
    std::string host_;
    int port_;
    std::atomic<bool> stop_flag_;
    std::thread server_thread_;
    int server_sock_;
    std::function<std::string(const HTTPRequest&)> handler_;

    void run() {
#ifdef _WIN32
        WSADATA wsaData;
        WSAStartup(MAKEWORD(2, 2), &wsaData);
#endif
        server_sock_ = socket(AF_INET, SOCK_STREAM, 0);
        if (server_sock_ < 0) return;
        int enable = 1;
        setsockopt(server_sock_, SOL_SOCKET, SO_REUSEADDR, (char*)&enable, sizeof(enable));

        struct sockaddr_in addr;
        addr.sin_family = AF_INET;
        addr.sin_port = htons(port_);
        addr.sin_addr.s_addr = host_ == "0.0.0.0" ? INADDR_ANY : inet_addr(host_.c_str());

        if (bind(server_sock_, (struct sockaddr*)&addr, sizeof(addr)) < 0) return;
        if (listen(server_sock_, 8) < 0) return;

        while (!stop_flag_) {
            struct sockaddr_in client_addr;
            socklen_t client_len = sizeof(client_addr);
            int client_sock =
#ifdef _WIN32
                accept(server_sock_, (struct sockaddr*)&client_addr, &client_len);
#else
                accept(server_sock_, (struct sockaddr*)&client_addr, &client_len);
#endif
            if (client_sock < 0) {
                if (stop_flag_) break;
                continue;
            }
            std::thread(&HTTPServer::handle_client, this, client_sock).detach();
        }
    }

    void handle_client(int client_sock) {
        char buffer[BUFFER_SIZE];
        int len = recv(client_sock, buffer, BUFFER_SIZE - 1, 0);
        if (len <= 0) {
#ifdef _WIN32
            closesocket(client_sock);
#else
            close(client_sock);
#endif
            return;
        }
        buffer[len] = 0;
        std::string req_str(buffer);
        HTTPRequest req = parse_http_request(req_str);

        std::string resp = handler_(req);

        send(client_sock, resp.c_str(), resp.length(), 0);
#ifdef _WIN32
        closesocket(client_sock);
#else
        close(client_sock);
#endif
    }
};

// ROS2 node wrapper (thread-safe state caching)
class RobotBridge : public rclcpp::Node {
public:
    RobotBridge(const std::string& node_name, const std::string& robot_ns)
        : Node(node_name) {
        // Subscriptions for status
        joint_state_sub_ = this->create_subscription<fairino_msgs::msg::JointState>(
            robot_ns + "/joint_states", 10,
            [this](const fairino_msgs::msg::JointState::SharedPtr msg) {
                std::lock_guard<std::mutex> lock(state_mutex_);
                last_joint_state_ = *msg;
                last_joint_state_time_ = this->now();
            });

        robot_state_sub_ = this->create_subscription<fairino_msgs::msg::RobotState>(
            robot_ns + "/robot_state", 10,
            [this](const fairino_msgs::msg::RobotState::SharedPtr msg) {
                std::lock_guard<std::mutex> lock(state_mutex_);
                last_robot_state_ = *msg;
                last_robot_state_time_ = this->now();
            });

        // Service clients
        move_client_ = this->create_client<fairino_msgs::srv::Move>(robot_ns + "/move");
        plan_client_ = this->create_client<fairino_msgs::srv::Plan>(robot_ns + "/plan");
        execute_traj_client_ = this->create_client<fairino_msgs::srv::ExecuteTrajectory>(robot_ns + "/execute_trajectory");
        task_status_client_ = this->create_client<fairino_msgs::srv::TaskStatus>(robot_ns + "/task_status");
    }

    // POST /robot/trajectory
    std::string execute_trajectory(const std::string& body) {
        // Expect JSON: { "trajectory": [ { "positions": [...], "timestamp": ... }, ... ] }
        // For brevity, we use a dumb parser for the example.
        fairino_msgs::srv::ExecuteTrajectory::Request req;
        std::vector<double> positions;
        std::vector<double> timestamps;
        if (!parse_trajectory_json(body, positions, timestamps))
            return http_response(400, "Bad Request", R"({"error":"Malformed trajectory JSON"})");

        if (!wait_for_service(execute_traj_client_))
            return http_response(500, "Service Unavailable", R"({"error":"Trajectory service unavailable"})");

        req.trajectory.joint_positions = positions;
        req.trajectory.time_from_start = timestamps;

        auto result = execute_traj_client_->async_send_request(std::make_shared<fairino_msgs::srv::ExecuteTrajectory::Request>(req));
        if (rclcpp::spin_until_future_complete(shared_from_this(), result, std::chrono::seconds(5)) != rclcpp::FutureReturnCode::SUCCESS) {
            return http_response(500, "Internal Server Error", R"({"error":"Trajectory execution failed"})");
        }
        auto resp = result.get();
        std::ostringstream oss;
        oss << R"({"success":)" << (resp->success ? "true" : "false") << R"(,"message":")" << resp->message << R"("})";
        return http_response(200, "OK", oss.str());
    }

    // POST /robot/move
    std::string move(const std::string& body) {
        // JSON: { "joint_angles": [...]} or { "pose": { "x":.., "y":.., "z":.., ... } }
        fairino_msgs::srv::Move::Request req;
        bool is_joint = false;
        if (body.find("joint_angles") != std::string::npos) {
            std::regex arr_re(R"("joint_angles"\s*:\s*\[\s*([^\]]*)\])");
            std::smatch m;
            if (std::regex_search(body, m, arr_re)) {
                std::istringstream iss(m[1].str());
                double v;
                char c;
                while (iss >> v) {
                    req.joint_angles.push_back(v);
                    iss >> c;
                }
                is_joint = true;
            }
        }
        if (!is_joint && body.find("pose") != std::string::npos) {
            std::regex pose_re(R"("pose"\s*:\s*\{([^}]*)\})");
            std::smatch m;
            if (std::regex_search(body, m, pose_re)) {
                std::regex f_re(R"("([a-z]+)"\s*:\s*([-\d.]+))");
                std::string pose_str = m[1].str();
                std::smatch pf;
                auto begin = std::sregex_iterator(pose_str.begin(), pose_str.end(), f_re);
                for (auto it = begin; it != std::sregex_iterator(); ++it) {
                    std::string key = (*it)[1];
                    double val = std::stod((*it)[2]);
                    if (key == "x") req.pose.x = val;
                    else if (key == "y") req.pose.y = val;
                    else if (key == "z") req.pose.z = val;
                    else if (key == "rx") req.pose.rx = val;
                    else if (key == "ry") req.pose.ry = val;
                    else if (key == "rz") req.pose.rz = val;
                }
            }
        }
        if (!is_joint && req.pose.x == 0 && req.pose.y == 0 && req.pose.z == 0)
            return http_response(400, "Bad Request", R"({"error":"Malformed move JSON"})");

        if (!wait_for_service(move_client_))
            return http_response(500, "Service Unavailable", R"({"error":"Move service unavailable"})");
        auto result = move_client_->async_send_request(std::make_shared<fairino_msgs::srv::Move::Request>(req));
        if (rclcpp::spin_until_future_complete(shared_from_this(), result, std::chrono::seconds(5)) != rclcpp::FutureReturnCode::SUCCESS) {
            return http_response(500, "Internal Server Error", R"({"error":"Move failed"})");
        }
        auto resp = result.get();
        std::ostringstream oss;
        oss << R"({"success":)" << (resp->success ? "true" : "false") << R"(,"message":")" << resp->message << R"("})";
        return http_response(200, "OK", oss.str());
    }

    // POST /robot/plan
    std::string plan(const std::string& body) {
        // JSON: { "goal": { ... } }
        fairino_msgs::srv::Plan::Request req;
        if (body.find("goal") == std::string::npos)
            return http_response(400, "Bad Request", R"({"error":"Malformed plan JSON"})");
        // For brevity, not parsing deeply.
        req.goal = body; // Assume the service parses it.
        if (!wait_for_service(plan_client_))
            return http_response(500, "Service Unavailable", R"({"error":"Plan service unavailable"})");

        auto result = plan_client_->async_send_request(std::make_shared<fairino_msgs::srv::Plan::Request>(req));
        if (rclcpp::spin_until_future_complete(shared_from_this(), result, std::chrono::seconds(5)) != rclcpp::FutureReturnCode::SUCCESS) {
            return http_response(500, "Internal Server Error", R"({"error":"Planning failed"})");
        }
        auto resp = result.get();
        std::ostringstream oss;
        oss << R"({"task_id":")" << resp->task_id << R"("})";
        return http_response(200, "OK", oss.str());
    }

    // GET /robot/status
    std::string get_status() {
        std::lock_guard<std::mutex> lock(state_mutex_);
        std::ostringstream oss;
        oss << R"({"joint_positions":[)";
        for (size_t i = 0; i < last_joint_state_.positions.size(); ++i) {
            if (i) oss << ",";
            oss << last_joint_state_.positions[i];
        }
        oss << R"(],"state":")" << last_robot_state_.state << R"("})";
        return http_response(200, "OK", oss.str());
    }

    // GET /robot/tasks/{taskId}
    std::string task_status(const std::string& task_id) {
        fairino_msgs::srv::TaskStatus::Request req;
        req.task_id = task_id;
        if (!wait_for_service(task_status_client_))
            return http_response(500, "Service Unavailable", R"({"error":"Task status service unavailable"})");
        auto result = task_status_client_->async_send_request(std::make_shared<fairino_msgs::srv::TaskStatus::Request>(req));
        if (rclcpp::spin_until_future_complete(shared_from_this(), result, std::chrono::seconds(5)) != rclcpp::FutureReturnCode::SUCCESS) {
            return http_response(500, "Internal Server Error", R"({"error":"Task status failed"})");
        }
        auto resp = result.get();
        std::ostringstream oss;
        oss << R"({"status":")" << resp->status << R"(","trajectory":[)";
        for (size_t i = 0; i < resp->trajectory.size(); ++i) {
            if (i) oss << ",";
            oss << resp->trajectory[i];
        }
        oss << "]}";
        return http_response(200, "OK", oss.str());
    }

private:
    rclcpp::Subscription<fairino_msgs::msg::JointState>::SharedPtr joint_state_sub_;
    rclcpp::Subscription<fairino_msgs::msg::RobotState>::SharedPtr robot_state_sub_;
    rclcpp::Client<fairino_msgs::srv::Move>::SharedPtr move_client_;
    rclcpp::Client<fairino_msgs::srv::Plan>::SharedPtr plan_client_;
    rclcpp::Client<fairino_msgs::srv::ExecuteTrajectory>::SharedPtr execute_traj_client_;
    rclcpp::Client<fairino_msgs::srv::TaskStatus>::SharedPtr task_status_client_;

    fairino_msgs::msg::JointState last_joint_state_;
    fairino_msgs::msg::RobotState last_robot_state_;
    rclcpp::Time last_joint_state_time_;
    rclcpp::Time last_robot_state_time_;
    std::mutex state_mutex_;

    bool parse_trajectory_json(const std::string& body, std::vector<double>& positions, std::vector<double>& timestamps) {
        // Minimalistic parser: expects e.g. {"trajectory":[{"positions":[1,2,3],"timestamp":0.1}, ...]}
        std::regex traj_re(R"(\{"positions"\s*:\s*\[([^\]]*)\]\s*,\s*"timestamp"\s*:\s*([0-9.eE+-]+)\})");
        auto begin = std::sregex_iterator(body.begin(), body.end(), traj_re);
        for (auto it = begin; it != std::sregex_iterator(); ++it) {
            std::string pos_str = (*it)[1];
            std::istringstream iss(pos_str);
            double v;
            char c;
            while (iss >> v) {
                positions.push_back(v);
                iss >> c;
            }
            timestamps.push_back(std::stod((*it)[2]));
        }
        return !positions.empty() && positions.size() == timestamps.size();
    }

    template<typename ClientT>
    bool wait_for_service(ClientT client, std::chrono::seconds timeout = std::chrono::seconds(2)) {
        return client->wait_for_service(timeout);
    }
};

int main(int argc, char* argv[]) {
    std::string ros_domain = getenv_or_default("ROS_DOMAIN_ID", "0");
    std::string robot_ns = getenv_or_default("FAIRINO_ROBOT_NS", "");
    std::string http_host = getenv_or_default("HTTP_SERVER_HOST", "0.0.0.0");
    int http_port = std::stoi(getenv_or_default("HTTP_SERVER_PORT", "8080"));

    setenv("ROS_DOMAIN_ID", ros_domain.c_str(), 1);

    rclcpp::init(argc, argv);
    auto robot = std::make_shared<RobotBridge>("fairino_http_bridge", robot_ns);

    HTTPServer server(http_host, http_port);

    server.start([robot](const HTTPRequest& req) -> std::string {
        try {
            if (req.method == "GET" && req.path == "/robot/status") {
                return robot->get_status();
            } else if (req.method == "POST" && req.path == "/robot/trajectory") {
                return robot->execute_trajectory(req.body);
            } else if (req.method == "POST" && req.path == "/robot/move") {
                return robot->move(req.body);
            } else if (req.method == "POST" && req.path == "/robot/plan") {
                return robot->plan(req.body);
            } else {
                std::regex task_re(R"(^/robot/tasks/([^/]+))");
                std::smatch m;
                if (req.method == "GET" && std::regex_match(req.path, m, task_re)) {
                    return robot->task_status(m[1]);
                }
            }
            return http_response(404, "Not Found", R"({"error":"Unknown endpoint"})");
        } catch (const std::exception& ex) {
            return http_response(500, "Internal Server Error", std::string(R"({"error":")") + ex.what() + R"("})");
        }
    });

    rclcpp::spin(robot);
    server.stop();
    rclcpp::shutdown();
    return 0;
}