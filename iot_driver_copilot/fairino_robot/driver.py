#include <cstdlib>
#include <cstring>
#include <iostream>
#include <thread>
#include <vector>
#include <atomic>
#include <sstream>
#include <chrono>
#include <mutex>
#include <condition_variable>
#include <unordered_map>
#include <functional>
#include <csignal>

// HTTP server
#include <netinet/in.h>
#include <unistd.h>
#include <sys/socket.h>

// ROS2
#include "rclcpp/rclcpp.hpp"
#include "sensor_msgs/msg/joint_state.hpp"
#include "std_msgs/msg/string.hpp"
#include "std_srvs/srv/trigger.hpp"
#include "std_msgs/msg/bool.hpp"
#include "std_msgs/msg/float64_multi_array.hpp"

// ----------- Config
#define ENV_STR(x) (std::getenv(x) ? std::getenv(x) : "")

struct Config {
    std::string ros_domain_id;
    std::string ros_namespace;
    std::string robot_ip;
    std::string http_host;
    int http_port;
    Config() {
        ros_domain_id = ENV_STR("ROS_DOMAIN_ID");
        ros_namespace = ENV_STR("ROS_NAMESPACE");
        robot_ip = ENV_STR("ROBOT_IP");
        http_host = ENV_STR("HTTP_HOST");
        http_port = std::atoi(ENV_STR("HTTP_PORT"));
        if(http_host.empty()) http_host = "0.0.0.0";
        if(http_port == 0) http_port = 8080;
    }
};

Config config;

// ----------- Globals for robot data
std::mutex robot_mutex;
sensor_msgs::msg::JointState::SharedPtr last_joint_state;
std::string last_robot_status;
std::string last_controller_state;
std::string last_planning_groups;

// For shutdown
std::atomic<bool> g_shutdown{false};

// ----------- ROS2 Node
class RobotBridgeNode : public rclcpp::Node {
public:
    RobotBridgeNode() : Node("fairino_robot_driver") {
        // Subscriptions
        joint_state_sub_ = this->create_subscription<sensor_msgs::msg::JointState>(
            "/joint_states", 10,
            [this](const sensor_msgs::msg::JointState::SharedPtr msg) {
                std::lock_guard<std::mutex> lk(robot_mutex);
                last_joint_state = msg;
            }
        );
        robot_status_sub_ = this->create_subscription<std_msgs::msg::String>(
            "/robot_status", 10,
            [](const std_msgs::msg::String::SharedPtr msg){
                std::lock_guard<std::mutex> lk(robot_mutex);
                last_robot_status = msg->data;
            }
        );
        controller_state_sub_ = this->create_subscription<std_msgs::msg::String>(
            "/controller_state", 10,
            [](const std_msgs::msg::String::SharedPtr msg){
                std::lock_guard<std::mutex> lk(robot_mutex);
                last_controller_state = msg->data;
            }
        );
        planning_groups_sub_ = this->create_subscription<std_msgs::msg::String>(
            "/planning_groups", 10,
            [](const std_msgs::msg::String::SharedPtr msg){
                std::lock_guard<std::mutex> lk(robot_mutex);
                last_planning_groups = msg->data;
            }
        );
        // Publishers for movement commands
        joint_cmd_pub_ = this->create_publisher<std_msgs::msg::Float64MultiArray>(
            "/joint_position_controller/commands", 10);
        moveit_cmd_pub_ = this->create_publisher<std_msgs::msg::String>(
            "/moveit2/planning_command", 10);
    }

    // For /plan endpoint (MoveIt2 planning execution)
    bool execute_plan(const std::string& plan_json) {
        std_msgs::msg::String msg;
        msg.data = plan_json;
        moveit_cmd_pub_->publish(msg);
        return true;
    }

    // For /jctrl endpoint (Joint position control)
    bool send_joint_positions(const std::vector<double>& positions) {
        std_msgs::msg::Float64MultiArray msg;
        msg.data = positions;
        joint_cmd_pub_->publish(msg);
        return true;
    }

private:
    rclcpp::Subscription<sensor_msgs::msg::JointState>::SharedPtr joint_state_sub_;
    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr robot_status_sub_;
    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr controller_state_sub_;
    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr planning_groups_sub_;
    rclcpp::Publisher<std_msgs::msg::Float64MultiArray>::SharedPtr joint_cmd_pub_;
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr moveit_cmd_pub_;
};

// ----------- Minimal HTTP server
struct HttpRequest {
    std::string method;
    std::string path;
    std::string body;
    std::unordered_map<std::string, std::string> headers;
};

struct HttpResponse {
    int status = 200;
    std::string content_type = "application/json";
    std::string body;
};

std::string http_status_message(int status) {
    switch(status) {
        case 200: return "OK";
        case 400: return "Bad Request";
        case 404: return "Not Found";
        case 405: return "Method Not Allowed";
        default:  return "Internal Server Error";
    }
}

void send_http_response(int client_fd, const HttpResponse& resp) {
    std::ostringstream oss;
    oss << "HTTP/1.1 " << resp.status << " " << http_status_message(resp.status) << "\r\n";
    oss << "Content-Type: " << resp.content_type << "\r\n";
    oss << "Access-Control-Allow-Origin: *\r\n";
    oss << "Content-Length: " << resp.body.size() << "\r\n\r\n";
    oss << resp.body;
    std::string out = oss.str();
    send(client_fd, out.data(), out.size(), 0);
}

bool parse_http_request(const std::string& raw, HttpRequest& req) {
    std::istringstream iss(raw);
    std::string line;
    if(!std::getline(iss, line)) return false;
    std::istringstream l1(line);
    l1 >> req.method >> req.path;
    while(std::getline(iss, line) && line != "\r") {
        if(line.empty() || line == "\r\n") break;
        auto pos = line.find(':');
        if(pos != std::string::npos) {
            std::string key = line.substr(0, pos);
            std::string value = line.substr(pos+1);
            while(!value.empty() && (value[0] == ' ' || value[0] == '\t')) value.erase(0,1);
            value.erase(value.find_last_not_of("\r\n")+1);
            req.headers[key] = value;
        }
    }
    if(req.headers.count("Content-Length")) {
        int len = std::stoi(req.headers["Content-Length"]);
        std::string body(len, '\0');
        iss.read(&body[0], len);
        req.body = body;
    }
    return true;
}

std::string json_escape(const std::string& s) {
    std::ostringstream oss;
    for(auto c : s) {
        if(c == '\"') oss << "\\\"";
        else if(c == '\\') oss << "\\\\";
        else if(c == '\n') oss << "\\n";
        else if(c == '\r') oss << "\\r";
        else oss << c;
    }
    return oss.str();
}

// ----------- Endpoint Handlers

HttpResponse handle_status() {
    std::lock_guard<std::mutex> lk(robot_mutex);
    std::ostringstream oss;
    oss << "{";
    oss << "\"robot_status\":\"" << json_escape(last_robot_status) << "\",";
    oss << "\"controller_state\":\"" << json_escape(last_controller_state) << "\",";
    oss << "\"planning_groups\":\"" << json_escape(last_planning_groups) << "\"";
    oss << "}";
    HttpResponse resp; resp.body = oss.str(); return resp;
}

HttpResponse handle_joints() {
    std::lock_guard<std::mutex> lk(robot_mutex);
    HttpResponse resp;
    if(!last_joint_state) {
        resp.status = 503;
        resp.body = "{\"error\":\"Joint state unavailable\"}";
        return resp;
    }
    std::ostringstream oss;
    oss << "{";
    oss << "\"name\":[";
    for(size_t i=0; i<last_joint_state->name.size(); ++i) {
        if(i) oss << ",";
        oss << "\"" << json_escape(last_joint_state->name[i]) << "\"";
    }
    oss << "],\"position\":[";
    for(size_t i=0; i<last_joint_state->position.size(); ++i) {
        if(i) oss << ",";
        oss << last_joint_state->position[i];
    }
    oss << "],\"velocity\":[";
    for(size_t i=0; i<last_joint_state->velocity.size(); ++i) {
        if(i) oss << ",";
        oss << last_joint_state->velocity[i];
    }
    oss << "],\"effort\":[";
    for(size_t i=0; i<last_joint_state->effort.size(); ++i) {
        if(i) oss << ",";
        oss << last_joint_state->effort[i];
    }
    oss << "]}";
    resp.body = oss.str();
    return resp;
}

HttpResponse handle_plan(RobotBridgeNode* node, const HttpRequest& req) {
    HttpResponse resp;
    if(req.body.empty()) {
        resp.status = 400;
        resp.body = "{\"error\":\"Missing request body\"}";
        return resp;
    }
    if(node->execute_plan(req.body)) {
        resp.body = "{\"result\":\"Plan command sent\"}";
    } else {
        resp.status = 500;
        resp.body = "{\"error\":\"Failed to send plan command\"}";
    }
    return resp;
}

HttpResponse handle_jctrl(RobotBridgeNode* node, const HttpRequest& req) {
    HttpResponse resp;
    // Expect body as JSON: {"positions":[...]}
    size_t pos = req.body.find("\"positions\"");
    if(pos == std::string::npos) {
        resp.status = 400;
        resp.body = "{\"error\":\"Missing 'positions' field in body\"}";
        return resp;
    }
    size_t arr_start = req.body.find('[', pos);
    size_t arr_end = req.body.find(']', arr_start);
    if(arr_start == std::string::npos || arr_end == std::string::npos) {
        resp.status = 400;
        resp.body = "{\"error\":\"Malformed 'positions' array\"}";
        return resp;
    }
    std::vector<double> positions;
    std::string arr = req.body.substr(arr_start+1, arr_end-arr_start-1);
    std::istringstream iss(arr);
    std::string v;
    while(std::getline(iss, v, ',')) {
        try {
            positions.push_back(std::stod(v));
        } catch(...) {}
    }
    if(node->send_joint_positions(positions)) {
        resp.body = "{\"result\":\"Joint positions sent\"}";
    } else {
        resp.status = 500;
        resp.body = "{\"error\":\"Failed to send joint positions\"}";
    }
    return resp;
}

// ----------- HTTP server loop
void http_server_thread(RobotBridgeNode* node) {
    int server_fd = socket(AF_INET, SOCK_STREAM, 0);
    if(server_fd < 0) {
        perror("socket");
        std::exit(1);
    }
    int opt = 1;
    setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    sockaddr_in addr;
    addr.sin_family = AF_INET;
    addr.sin_port = htons(config.http_port);
    addr.sin_addr.s_addr = inet_addr(config.http_host.c_str());
    if(bind(server_fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        perror("bind");
        close(server_fd);
        std::exit(1);
    }
    if(listen(server_fd, 5) < 0) {
        perror("listen");
        close(server_fd);
        std::exit(1);
    }
    while(!g_shutdown) {
        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(server_fd, &rfds);
        struct timeval tv;
        tv.tv_sec = 1; tv.tv_usec = 0;
        int rv = select(server_fd+1, &rfds, NULL, NULL, &tv);
        if(rv == 0) continue;
        if(rv < 0) break;
        int client_fd = accept(server_fd, NULL, NULL);
        if(client_fd < 0) continue;
        std::thread([node, client_fd]() {
            char buf[4096];
            ssize_t n = recv(client_fd, buf, sizeof(buf)-1, 0);
            if(n <= 0) { close(client_fd); return; }
            buf[n] = 0;
            HttpRequest req;
            if(!parse_http_request(buf, req)) {
                HttpResponse resp; resp.status = 400; resp.body = "{\"error\":\"Bad request\"}";
                send_http_response(client_fd, resp);
                close(client_fd);
                return;
            }
            HttpResponse resp;
            if(req.method == "GET" && req.path == "/status") {
                resp = handle_status();
            } else if(req.method == "GET" && req.path == "/joints") {
                resp = handle_joints();
            } else if(req.method == "POST" && req.path == "/plan") {
                resp = handle_plan(node, req);
            } else if(req.method == "POST" && req.path == "/jctrl") {
                resp = handle_jctrl(node, req);
            } else {
                resp.status = 404;
                resp.body = "{\"error\":\"Not found\"}";
            }
            send_http_response(client_fd, resp);
            close(client_fd);
        }).detach();
    }
    close(server_fd);
}

// ----------- Signal handler
void handle_signal(int) {
    g_shutdown = true;
}

// ----------- Main
int main(int argc, char** argv) {
    std::signal(SIGINT, handle_signal);
    std::signal(SIGTERM, handle_signal);
    rclcpp::init(argc, argv);
    auto node = std::make_shared<RobotBridgeNode>();
    std::thread th([&](){ http_server_thread(node.get()); });
    while(rclcpp::ok() && !g_shutdown) {
        rclcpp::spin_some(node);
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    g_shutdown = true;
    th.join();
    rclcpp::shutdown();
    return 0;
}