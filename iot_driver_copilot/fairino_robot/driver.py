#include <iostream>
#include <cstdlib>
#include <string>
#include <thread>
#include <chrono>
#include <vector>
#include <map>
#include <sstream>
#include <memory>
#include <cstring>
#include <mutex>
#include <condition_variable>
#include <nlohmann/json.hpp>

// ROS2 headers
#include "rclcpp/rclcpp.hpp"
#include "sensor_msgs/msg/joint_state.hpp"
#include "std_msgs/msg/string.hpp"
#include "std_srvs/srv/trigger.hpp"
#include "std_srvs/srv/empty.hpp"
#include "geometry_msgs/msg/pose.hpp"
#include "geometry_msgs/msg/twist.hpp"

// HTTP Server (simple, header-only, no external lib)
#include <sys/types.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <unistd.h>

using json = nlohmann::json;

// ========== Environment Variable Helper ==========
std::string getenv_or(const char* name, const std::string& fallback) {
    const char* val = std::getenv(name);
    return val ? std::string(val) : fallback;
}

// ========== ROS2 Node Wrapper ==========

class RobotState : public rclcpp::Node {
public:
    RobotState(const std::string& node_name)
        : Node(node_name)
    {
        joint_state_sub_ = this->create_subscription<sensor_msgs::msg::JointState>(
            getenv_or("FAIRINO_JOINT_STATE_TOPIC", "/joint_states"), 10,
            std::bind(&RobotState::joint_state_callback, this, std::placeholders::_1)
        );
        status_sub_ = this->create_subscription<std_msgs::msg::String>(
            getenv_or("FAIRINO_STATUS_TOPIC", "/robot_status"), 10,
            std::bind(&RobotState::status_callback, this, std::placeholders::_1)
        );
        planning_groups_sub_ = this->create_subscription<std_msgs::msg::String>(
            getenv_or("FAIRINO_PLANNING_GROUPS_TOPIC", "/planning_groups"), 10,
            std::bind(&RobotState::planning_groups_callback, this, std::placeholders::_1)
        );
        controller_state_sub_ = this->create_subscription<std_msgs::msg::String>(
            getenv_or("FAIRINO_CONTROLLER_STATE_TOPIC", "/controller_state"), 10,
            std::bind(&RobotState::controller_state_callback, this, std::placeholders::_1)
        );
        // For movement and control - services and publishers
        plan_client_ = this->create_client<std_srvs::srv::Trigger>(
            getenv_or("FAIRINO_PLAN_SERVICE", "/plan_execution")
        );
        joint_ctrl_pub_ = this->create_publisher<sensor_msgs::msg::JointState>(
            getenv_or("FAIRINO_JOINT_CTRL_TOPIC", "/joint_ctrl"), 10
        );
    }

    // --- Data Storage ---
    std::mutex data_mutex_;
    sensor_msgs::msg::JointState last_joint_state_;
    std::string last_status_;
    std::string last_planning_groups_;
    std::string last_controller_state_;

    // --- Callbacks ---
    void joint_state_callback(const sensor_msgs::msg::JointState::SharedPtr msg) {
        std::lock_guard<std::mutex> lock(data_mutex_);
        last_joint_state_ = *msg;
    }
    void status_callback(const std_msgs::msg::String::SharedPtr msg) {
        std::lock_guard<std::mutex> lock(data_mutex_);
        last_status_ = msg->data;
    }
    void planning_groups_callback(const std_msgs::msg::String::SharedPtr msg) {
        std::lock_guard<std::mutex> lock(data_mutex_);
        last_planning_groups_ = msg->data;
    }
    void controller_state_callback(const std_msgs::msg::String::SharedPtr msg) {
        std::lock_guard<std::mutex> lock(data_mutex_);
        last_controller_state_ = msg->data;
    }

    // --- Data Getters for HTTP ---
    json get_status_json() {
        std::lock_guard<std::mutex> lock(data_mutex_);
        json j;
        j["status"] = last_status_;
        j["controller_state"] = last_controller_state_;
        j["planning_groups"] = last_planning_groups_;
        return j;
    }

    json get_joints_json() {
        std::lock_guard<std::mutex> lock(data_mutex_);
        json j;
        j["joint_names"] = last_joint_state_.name;
        j["positions"] = last_joint_state_.position;
        j["velocities"] = last_joint_state_.velocity;
        j["efforts"] = last_joint_state_.effort;
        return j;
    }

    // --- Plan Execution (POST /plan) ---
    bool execute_plan() {
        if(!plan_client_->wait_for_service(std::chrono::milliseconds(300))) {
            return false;
        }
        auto req = std::make_shared<std_srvs::srv::Trigger::Request>();
        auto future = plan_client_->async_send_request(req);
        if(future.wait_for(std::chrono::seconds(3)) == std::future_status::ready) {
            auto resp = future.get();
            return resp->success;
        }
        return false;
    }

    // --- Joint Control (POST /jctrl) ---
    bool set_joint_positions(const std::vector<std::string>& names, const std::vector<double>& positions) {
        if(names.size() != positions.size() || names.empty())
            return false;
        auto msg = sensor_msgs::msg::JointState();
        msg.name = names;
        msg.position = positions;
        joint_ctrl_pub_->publish(msg);
        return true;
    }

private:
    rclcpp::Subscription<sensor_msgs::msg::JointState>::SharedPtr joint_state_sub_;
    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr status_sub_;
    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr planning_groups_sub_;
    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr controller_state_sub_;
    rclcpp::Client<std_srvs::srv::Trigger>::SharedPtr plan_client_;
    rclcpp::Publisher<sensor_msgs::msg::JointState>::SharedPtr joint_ctrl_pub_;
};

// ========== Minimal HTTP Server ==========

class SimpleHTTPServer {
public:
    SimpleHTTPServer(const std::string& host, int port)
        : host_(host), port_(port), sockfd_(-1) {}

    bool start(std::function<void(int, const std::string&, const std::string&, const std::map<std::string, std::string>&, const std::string&)> handler) {
        sockfd_ = socket(AF_INET, SOCK_STREAM, 0);
        if (sockfd_ < 0) return false;

        int opt = 1;
        setsockopt(sockfd_, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

        struct sockaddr_in server_addr;
        std::memset(&server_addr, 0, sizeof(server_addr));
        server_addr.sin_family = AF_INET;
        server_addr.sin_addr.s_addr = INADDR_ANY;
        server_addr.sin_port = htons(port_);

        if (bind(sockfd_, (struct sockaddr*)&server_addr, sizeof(server_addr)) < 0) {
            close(sockfd_);
            return false;
        }
        if (listen(sockfd_, 10) < 0) {
            close(sockfd_);
            return false;
        }

        std::thread([this, handler]() { this->accept_loop(handler); }).detach();
        return true;
    }

    void stop() {
        if (sockfd_ > 0) close(sockfd_);
    }

private:
    std::string host_;
    int port_;
    int sockfd_;

    void accept_loop(std::function<void(int, const std::string&, const std::string&, const std::map<std::string, std::string>&, const std::string&)> handler) {
        while (true) {
            int client = accept(sockfd_, nullptr, nullptr);
            if (client < 0) continue;

            std::thread([handler, client]() {
                char buffer[8192];
                ssize_t received = recv(client, buffer, sizeof(buffer), 0);
                if (received <= 0) { close(client); return; }
                std::string request(buffer, received);

                std::istringstream stream(request);
                std::string req_line;
                getline(stream, req_line);
                std::istringstream req_line_stream(req_line);
                std::string method, path, version;
                req_line_stream >> method >> path >> version;

                std::map<std::string, std::string> headers;
                std::string header;
                while (getline(stream, header) && header != "\r") {
                    auto colon = header.find(':');
                    if (colon != std::string::npos) {
                        std::string name = header.substr(0, colon);
                        std::string value = header.substr(colon + 1);
                        while (!value.empty() && (value[0] == ' ' || value[0] == '\t')) value.erase(0, 1);
                        if (!value.empty() && value.back() == '\r') value.pop_back();
                        headers[name] = value;
                    }
                }
                std::string body;
                if (headers.count("Content-Length")) {
                    int len = std::stoi(headers["Content-Length"]);
                    body.resize(len);
                    stream.read(&body[0], len);
                }

                handler(client, method, path, headers, body);
                close(client);
            }).detach();
        }
    }
};

// ========== HTTP Handler ==========
void send_response(int client, int code, const std::string& content_type, const std::string& body) {
    std::ostringstream oss;
    oss << "HTTP/1.1 " << code << " " << (code == 200 ? "OK" : "ERROR") << "\r\n";
    oss << "Content-Type: " << content_type << "\r\n";
    oss << "Content-Length: " << body.size() << "\r\n";
    oss << "Access-Control-Allow-Origin: *\r\n";
    oss << "\r\n";
    oss << body;
    std::string resp = oss.str();
    send(client, resp.data(), resp.size(), 0);
}

void handle_http_request(int client, const std::string& method, const std::string& path,
                        const std::map<std::string, std::string>& headers,
                        const std::string& body,
                        std::shared_ptr<RobotState> robot) {
    try {
        if (method == "GET" && path == "/status") {
            auto status = robot->get_status_json();
            send_response(client, 200, "application/json", status.dump());
        } else if (method == "GET" && path == "/joints") {
            auto joints = robot->get_joints_json();
            send_response(client, 200, "application/json", joints.dump());
        } else if (method == "POST" && path == "/plan") {
            bool ok = robot->execute_plan();
            json resp;
            resp["success"] = ok;
            send_response(client, 200, "application/json", resp.dump());
        } else if (method == "POST" && path == "/jctrl") {
            json req = json::parse(body);
            if (!req.contains("joint_names") || !req.contains("positions") ||
                !req["joint_names"].is_array() || !req["positions"].is_array() ||
                req["joint_names"].size() != req["positions"].size()) {
                send_response(client, 400, "application/json", R"({"error":"invalid input"})");
                return;
            }
            std::vector<std::string> names = req["joint_names"].get<std::vector<std::string>>();
            std::vector<double> positions = req["positions"].get<std::vector<double>>();
            bool ok = robot->set_joint_positions(names, positions);
            json resp;
            resp["success"] = ok;
            send_response(client, 200, "application/json", resp.dump());
        } else {
            send_response(client, 404, "application/json", R"({"error":"not found"})");
        }
    } catch (...) {
        send_response(client, 500, "application/json", R"({"error":"internal server error"})");
    }
}

// ========== Main Entrypoint ==========
int main(int argc, char* argv[]) {
    // Environment - config
    std::string ros_domain = getenv_or("ROS_DOMAIN_ID", "0");
    std::string ros_namespace = getenv_or("ROS_NAMESPACE", "");
    std::string http_host = getenv_or("HTTP_HOST", "0.0.0.0");
    int http_port = std::stoi(getenv_or("HTTP_PORT", "8080"));

    setenv("ROS_DOMAIN_ID", ros_domain.c_str(), 1);
    if (!ros_namespace.empty())
        setenv("ROS_NAMESPACE", ros_namespace.c_str(), 1);

    rclcpp::init(argc, argv);
    auto robot = std::make_shared<RobotState>("fairino_robot_http_node");

    SimpleHTTPServer server(http_host, http_port);
    if (!server.start([robot](int client, const std::string& method, const std::string& path,
                              const std::map<std::string, std::string>& headers, const std::string& body) {
        handle_http_request(client, method, path, headers, body, robot);
    })) {
        std::cerr << "Failed to start HTTP server on " << http_host << ":" << http_port << std::endl;
        return 1;
    }

    std::cout << "Fairino Robot HTTP server running on " << http_host << ":" << http_port << std::endl;
    rclcpp::spin(robot);
    rclcpp::shutdown();
    server.stop();
    return 0;
}
