#include <iostream>
#include <cstdlib>
#include <string>
#include <thread>
#include <memory>
#include <map>
#include <vector>
#include <chrono>
#include <sstream>
#include <algorithm>
#include <mutex>
#include <condition_variable>
#include <atomic>
#include <csignal>

// HTTP Server dependencies
#include <asio.hpp>

// ROS2 dependencies
#include <rclcpp/rclcpp.hpp>
#include <std_msgs/msg/string.hpp>

// Custom Fairino message types (replace with actual message/service definitions)
#include "fairino_msgs/msg/robot_state.hpp"
#include "fairino_msgs/srv/plan_motion.hpp"
#include "fairino_msgs/srv/execute_motion.hpp"
#include "fairino_msgs/srv/plan_and_execute.hpp"
#include "fairino_msgs/srv/end_effector_command.hpp"
#include "fairino_msgs/srv/cyclic_grasp.hpp"
#include "fairino_msgs/srv/controller_command.hpp"

// Utility for environment variable
std::string get_env(const char* key, const std::string& def = "") {
    const char* val = std::getenv(key);
    return val ? std::string(val) : def;
}

// HTTP Response helpers
std::string http_ok_json(const std::string& json) {
    std::ostringstream oss;
    oss << "HTTP/1.1 200 OK\r\n"
        << "Content-Type: application/json\r\n"
        << "Access-Control-Allow-Origin: *\r\n"
        << "Content-Length: " << json.size() << "\r\n\r\n"
        << json;
    return oss.str();
}

std::string http_bad_request(const std::string& msg) {
    std::ostringstream oss;
    oss << "HTTP/1.1 400 Bad Request\r\n"
        << "Content-Type: application/json\r\n"
        << "Access-Control-Allow-Origin: *\r\n"
        << "Content-Length: " << msg.size() << "\r\n\r\n"
        << msg;
    return oss.str();
}

std::string http_method_not_allowed() {
    std::string msg = "{\"error\": \"Method Not Allowed\"}";
    std::ostringstream oss;
    oss << "HTTP/1.1 405 Method Not Allowed\r\n"
        << "Content-Type: application/json\r\n"
        << "Access-Control-Allow-Origin: *\r\n"
        << "Content-Length: " << msg.size() << "\r\n\r\n"
        << msg;
    return oss.str();
}

std::string http_internal_error(const std::string& msg) {
    std::ostringstream oss;
    oss << "HTTP/1.1 500 Internal Server Error\r\n"
        << "Content-Type: application/json\r\n"
        << "Access-Control-Allow-Origin: *\r\n"
        << "Content-Length: " << msg.size() << "\r\n\r\n"
        << msg;
    return oss.str();
}

std::string http_not_found() {
    std::string msg = "{\"error\": \"Not Found\"}";
    std::ostringstream oss;
    oss << "HTTP/1.1 404 Not Found\r\n"
        << "Content-Type: application/json\r\n"
        << "Access-Control-Allow-Origin: *\r\n"
        << "Content-Length: " << msg.size() << "\r\n\r\n"
        << msg;
    return oss.str();
}

// HTTP Request parsing
struct HttpRequest {
    std::string method;
    std::string path;
    std::map<std::string, std::string> headers;
    std::string body;
};

HttpRequest parse_http_request(const std::string& req) {
    HttpRequest r;
    std::istringstream iss(req);
    std::string line;
    std::getline(iss, line);
    std::istringstream ls(line);
    ls >> r.method;
    ls >> r.path;
    while (std::getline(iss, line) && line != "\r") {
        auto pos = line.find(':');
        if (pos != std::string::npos) {
            auto key = line.substr(0, pos);
            auto value = line.substr(pos + 1);
            value.erase(0, value.find_first_not_of(" \t"));
            value.erase(value.find_last_not_of("\r\n") + 1);
            std::transform(key.begin(), key.end(), key.begin(), ::tolower);
            r.headers[key] = value;
        }
    }
    // Read body if present
    auto it = r.headers.find("content-length");
    if (it != r.headers.end()) {
        size_t len = std::stoi(it->second);
        r.body.resize(len);
        iss.read(&r.body[0], len);
    }
    return r;
}

// ------------ ROS2 Client Node ---------------
class FairinoROS2Client : public rclcpp::Node {
public:
    FairinoROS2Client(const std::string& node_name)
        : Node(node_name)
    {
        // Services
        plan_cli_ = this->create_client<fairino_msgs::srv::PlanMotion>("plan_motion");
        exec_cli_ = this->create_client<fairino_msgs::srv::ExecuteMotion>("execute_motion");
        planexec_cli_ = this->create_client<fairino_msgs::srv::PlanAndExecute>("plan_and_execute");
        eef_cli_ = this->create_client<fairino_msgs::srv::EndEffectorCommand>("end_effector_command");
        grasp_cli_ = this->create_client<fairino_msgs::srv::CyclicGrasp>("cyclic_grasp");
        ctrl_cli_ = this->create_client<fairino_msgs::srv::ControllerCommand>("controller_command");
        // State subscription
        state_sub_ = this->create_subscription<fairino_msgs::msg::RobotState>(
            "robot_state", 10,
            [this](const fairino_msgs::msg::RobotState::SharedPtr msg) {
                std::lock_guard<std::mutex> lk(state_mutex_);
                last_state_ = *msg;
                last_state_time_ = this->now();
            }
        );
    }

    fairino_msgs::msg::RobotState get_latest_state() {
        std::lock_guard<std::mutex> lk(state_mutex_);
        return last_state_;
    }

    // --- Service wrappers ---

    bool plan_motion(const std::string& json, std::string& result_json) {
        auto req = std::make_shared<fairino_msgs::srv::PlanMotion::Request>();
        // Populate req from json
        if (!parse_plan_motion_json(json, req)) {
            result_json = "{\"error\":\"Invalid request body\"}";
            return false;
        }
        if (!plan_cli_->wait_for_service(std::chrono::milliseconds(500))) {
            result_json = "{\"error\":\"ROS2 plan_motion service not available\"}";
            return false;
        }
        auto fut = plan_cli_->async_send_request(req);
        auto status = rclcpp::spin_until_future_complete(this->get_node_base_interface(), fut, std::chrono::seconds(5));
        if (status != rclcpp::FutureReturnCode::SUCCESS) {
            result_json = "{\"error\":\"plan_motion service call failed\"}";
            return false;
        }
        auto resp = fut.get();
        result_json = plan_motion_response_to_json(resp);
        return true;
    }

    bool execute_motion(const std::string& json, std::string& result_json) {
        auto req = std::make_shared<fairino_msgs::srv::ExecuteMotion::Request>();
        if (!parse_execute_motion_json(json, req)) {
            result_json = "{\"error\":\"Invalid request body\"}";
            return false;
        }
        if (!exec_cli_->wait_for_service(std::chrono::milliseconds(500))) {
            result_json = "{\"error\":\"ROS2 execute_motion service not available\"}";
            return false;
        }
        auto fut = exec_cli_->async_send_request(req);
        auto status = rclcpp::spin_until_future_complete(this->get_node_base_interface(), fut, std::chrono::seconds(5));
        if (status != rclcpp::FutureReturnCode::SUCCESS) {
            result_json = "{\"error\":\"execute_motion service call failed\"}";
            return false;
        }
        auto resp = fut.get();
        result_json = execute_motion_response_to_json(resp);
        return true;
    }

    bool plan_and_execute(const std::string& json, std::string& result_json) {
        auto req = std::make_shared<fairino_msgs::srv::PlanAndExecute::Request>();
        if (!parse_plan_and_execute_json(json, req)) {
            result_json = "{\"error\":\"Invalid request body\"}";
            return false;
        }
        if (!planexec_cli_->wait_for_service(std::chrono::milliseconds(500))) {
            result_json = "{\"error\":\"ROS2 plan_and_execute service not available\"}";
            return false;
        }
        auto fut = planexec_cli_->async_send_request(req);
        auto status = rclcpp::spin_until_future_complete(this->get_node_base_interface(), fut, std::chrono::seconds(5));
        if (status != rclcpp::FutureReturnCode::SUCCESS) {
            result_json = "{\"error\":\"plan_and_execute service call failed\"}";
            return false;
        }
        auto resp = fut.get();
        result_json = plan_and_execute_response_to_json(resp);
        return true;
    }

    bool end_effector_command(const std::string& json, std::string& result_json) {
        auto req = std::make_shared<fairino_msgs::srv::EndEffectorCommand::Request>();
        if (!parse_end_effector_command_json(json, req)) {
            result_json = "{\"error\":\"Invalid request body\"}";
            return false;
        }
        if (!eef_cli_->wait_for_service(std::chrono::milliseconds(500))) {
            result_json = "{\"error\":\"ROS2 end_effector_command service not available\"}";
            return false;
        }
        auto fut = eef_cli_->async_send_request(req);
        auto status = rclcpp::spin_until_future_complete(this->get_node_base_interface(), fut, std::chrono::seconds(5));
        if (status != rclcpp::FutureReturnCode::SUCCESS) {
            result_json = "{\"error\":\"end_effector_command service call failed\"}";
            return false;
        }
        auto resp = fut.get();
        result_json = end_effector_command_response_to_json(resp);
        return true;
    }

    bool cyclic_grasp(const std::string& json, std::string& result_json) {
        auto req = std::make_shared<fairino_msgs::srv::CyclicGrasp::Request>();
        if (!parse_cyclic_grasp_json(json, req)) {
            result_json = "{\"error\":\"Invalid request body\"}";
            return false;
        }
        if (!grasp_cli_->wait_for_service(std::chrono::milliseconds(500))) {
            result_json = "{\"error\":\"ROS2 cyclic_grasp service not available\"}";
            return false;
        }
        auto fut = grasp_cli_->async_send_request(req);
        auto status = rclcpp::spin_until_future_complete(this->get_node_base_interface(), fut, std::chrono::seconds(5));
        if (status != rclcpp::FutureReturnCode::SUCCESS) {
            result_json = "{\"error\":\"cyclic_grasp service call failed\"}";
            return false;
        }
        auto resp = fut.get();
        result_json = cyclic_grasp_response_to_json(resp);
        return true;
    }

    bool controller_command(const std::string& json, std::string& result_json) {
        auto req = std::make_shared<fairino_msgs::srv::ControllerCommand::Request>();
        if (!parse_controller_command_json(json, req)) {
            result_json = "{\"error\":\"Invalid request body\"}";
            return false;
        }
        if (!ctrl_cli_->wait_for_service(std::chrono::milliseconds(500))) {
            result_json = "{\"error\":\"ROS2 controller_command service not available\"}";
            return false;
        }
        auto fut = ctrl_cli_->async_send_request(req);
        auto status = rclcpp::spin_until_future_complete(this->get_node_base_interface(), fut, std::chrono::seconds(5));
        if (status != rclcpp::FutureReturnCode::SUCCESS) {
            result_json = "{\"error\":\"controller_command service call failed\"}";
            return false;
        }
        auto resp = fut.get();
        result_json = controller_command_response_to_json(resp);
        return true;
    }

    bool get_state_json(std::string& result_json) {
        fairino_msgs::msg::RobotState state = get_latest_state();
        result_json = robot_state_to_json(state);
        return true;
    }

private:
    // Placeholders for ROS2 clients and subs
    rclcpp::Client<fairino_msgs::srv::PlanMotion>::SharedPtr plan_cli_;
    rclcpp::Client<fairino_msgs::srv::ExecuteMotion>::SharedPtr exec_cli_;
    rclcpp::Client<fairino_msgs::srv::PlanAndExecute>::SharedPtr planexec_cli_;
    rclcpp::Client<fairino_msgs::srv::EndEffectorCommand>::SharedPtr eef_cli_;
    rclcpp::Client<fairino_msgs::srv::CyclicGrasp>::SharedPtr grasp_cli_;
    rclcpp::Client<fairino_msgs::srv::ControllerCommand>::SharedPtr ctrl_cli_;
    rclcpp::Subscription<fairino_msgs::msg::RobotState>::SharedPtr state_sub_;
    // State cache
    fairino_msgs::msg::RobotState last_state_;
    rclcpp::Time last_state_time_;
    std::mutex state_mutex_;

    // --- JSON parsing and generation placeholders ---

    bool parse_plan_motion_json(const std::string&, fairino_msgs::srv::PlanMotion::Request::SharedPtr&) {
        // TODO: Implement actual parsing
        return true;
    }
    std::string plan_motion_response_to_json(const fairino_msgs::srv::PlanMotion::Response::SharedPtr&) {
        // TODO: Implement actual serialization
        return "{\"result\": \"plan_motion response\"}";
    }
    bool parse_execute_motion_json(const std::string&, fairino_msgs::srv::ExecuteMotion::Request::SharedPtr&) {
        // TODO: Implement actual parsing
        return true;
    }
    std::string execute_motion_response_to_json(const fairino_msgs::srv::ExecuteMotion::Response::SharedPtr&) {
        // TODO: Implement actual serialization
        return "{\"result\": \"execute_motion response\"}";
    }
    bool parse_plan_and_execute_json(const std::string&, fairino_msgs::srv::PlanAndExecute::Request::SharedPtr&) {
        // TODO: Implement actual parsing
        return true;
    }
    std::string plan_and_execute_response_to_json(const fairino_msgs::srv::PlanAndExecute::Response::SharedPtr&) {
        // TODO: Implement actual serialization
        return "{\"result\": \"plan_and_execute response\"}";
    }
    bool parse_end_effector_command_json(const std::string&, fairino_msgs::srv::EndEffectorCommand::Request::SharedPtr&) {
        // TODO: Implement actual parsing
        return true;
    }
    std::string end_effector_command_response_to_json(const fairino_msgs::srv::EndEffectorCommand::Response::SharedPtr&) {
        // TODO: Implement actual serialization
        return "{\"result\": \"end_effector_command response\"}";
    }
    bool parse_cyclic_grasp_json(const std::string&, fairino_msgs::srv::CyclicGrasp::Request::SharedPtr&) {
        // TODO: Implement actual parsing
        return true;
    }
    std::string cyclic_grasp_response_to_json(const fairino_msgs::srv::CyclicGrasp::Response::SharedPtr&) {
        // TODO: Implement actual serialization
        return "{\"result\": \"cyclic_grasp response\"}";
    }
    bool parse_controller_command_json(const std::string&, fairino_msgs::srv::ControllerCommand::Request::SharedPtr&) {
        // TODO: Implement actual parsing
        return true;
    }
    std::string controller_command_response_to_json(const fairino_msgs::srv::ControllerCommand::Response::SharedPtr&) {
        // TODO: Implement actual serialization
        return "{\"result\": \"controller_command response\"}";
    }
    std::string robot_state_to_json(const fairino_msgs::msg::RobotState&) {
        // TODO: Implement actual serialization
        return "{\"state\": \"robot_state data\"}";
    }
};

// ----------- HTTP Server Implementation ---------------

class HttpServer {
public:
    HttpServer(asio::io_context& io,
               std::shared_ptr<FairinoROS2Client> ros2_client,
               const std::string& host,
               unsigned short port)
        : acceptor_(io, asio::ip::tcp::endpoint(asio::ip::make_address(host), port)),
          ros2_client_(ros2_client)
    {
        do_accept();
    }

private:
    asio::ip::tcp::acceptor acceptor_;
    std::shared_ptr<FairinoROS2Client> ros2_client_;

    void do_accept() {
        acceptor_.async_accept(
            [this](std::error_code ec, asio::ip::tcp::socket socket) {
                if (!ec) {
                    std::thread([this, sock = std::move(socket)]() mutable {
                        handle_session(std::move(sock));
                    }).detach();
                }
                do_accept();
            }
        );
    }

    void handle_session(asio::ip::tcp::socket socket) {
        try {
            asio::streambuf buf;
            asio::read_until(socket, buf, "\r\n\r\n");
            std::istream req_stream(&buf);
            std::string req_str;
            std::getline(req_stream, req_str, '\0');
            HttpRequest req = parse_http_request(req_str);

            std::string resp;
            if (req.method == "GET" && req.path == "/state") {
                std::string json;
                if (ros2_client_->get_state_json(json)) {
                    resp = http_ok_json(json);
                } else {
                    resp = http_internal_error("{\"error\":\"Failed to retrieve robot state\"}");
                }
            } else if (req.method == "POST" && req.path == "/plan") {
                std::string json;
                if (ros2_client_->plan_motion(req.body, json)) {
                    resp = http_ok_json(json);
                } else {
                    resp = http_bad_request(json);
                }
            } else if (req.method == "POST" && req.path == "/exec") {
                std::string json;
                if (ros2_client_->execute_motion(req.body, json)) {
                    resp = http_ok_json(json);
                } else {
                    resp = http_bad_request(json);
                }
            } else if (req.method == "POST" && req.path == "/planexec") {
                std::string json;
                if (ros2_client_->plan_and_execute(req.body, json)) {
                    resp = http_ok_json(json);
                } else {
                    resp = http_bad_request(json);
                }
            } else if (req.method == "POST" && req.path == "/eef") {
                std::string json;
                if (ros2_client_->end_effector_command(req.body, json)) {
                    resp = http_ok_json(json);
                } else {
                    resp = http_bad_request(json);
                }
            } else if (req.method == "POST" && req.path == "/grasp") {
                std::string json;
                if (ros2_client_->cyclic_grasp(req.body, json)) {
                    resp = http_ok_json(json);
                } else {
                    resp = http_bad_request(json);
                }
            } else if (req.method == "POST" && req.path == "/ctrl") {
                std::string json;
                if (ros2_client_->controller_command(req.body, json)) {
                    resp = http_ok_json(json);
                } else {
                    resp = http_bad_request(json);
                }
            } else {
                resp = http_not_found();
            }
            asio::write(socket, asio::buffer(resp));
        } catch (const std::exception& ex) {
            std::string resp = http_internal_error("{\"error\":\"Internal server error\"}");
            asio::write(socket, asio::buffer(resp));
        }
        socket.close();
    }
};

// ------------ Main Entrypoint ------------

std::atomic<bool> running{true};

void sig_handler(int) {
    running = false;
}

int main(int argc, char* argv[]) {
    // Read environment variables
    std::string ros2_domain = get_env("ROS_DOMAIN_ID", "0");
    std::string ros2_ns = get_env("ROS_NAMESPACE", "");
    std::string http_host = get_env("FAIRINO_HTTP_HOST", "0.0.0.0");
    unsigned short http_port = static_cast<unsigned short>(std::stoi(get_env("FAIRINO_HTTP_PORT", "8080")));

    // Set up signal handling for clean exit
    std::signal(SIGINT, sig_handler);
    std::signal(SIGTERM, sig_handler);

    // Init ROS2
    rclcpp::init(argc, argv);

    // ROS2 node
    auto ros2_client = std::make_shared<FairinoROS2Client>("fairino_http_driver");

    // IO context for HTTP server
    asio::io_context io;

    // HTTP server
    HttpServer server(io, ros2_client, http_host, http_port);

    // ROS2 run thread
    std::thread ros2_thread([&]() {
        rclcpp::spin(ros2_client);
    });

    // Main IO loop
    while (running) {
        io.poll();
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }

    rclcpp::shutdown();
    ros2_thread.join();
    return 0;
}