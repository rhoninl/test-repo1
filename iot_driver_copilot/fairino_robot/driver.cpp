#include <cstdlib>
#include <memory>
#include <string>
#include <vector>
#include <map>
#include <thread>
#include <mutex>
#include <sstream>
#include <iostream>
#include <functional>
#include <algorithm>
#include <chrono>

#include "httplib.h"
#include "rclcpp/rclcpp.hpp"
#include "std_msgs/msg/string.hpp"
#include "sensor_msgs/msg/joint_state.hpp"
#include "geometry_msgs/msg/pose.hpp"

// NOTE: You need to add dependencies to httplib.h (https://github.com/yhirose/cpp-httplib) and ROS2 client libraries.

using namespace std::chrono_literals;

// Utility: get environment variable with fallback
std::string getenv_or(const char* key, const char* def) {
    const char* v = std::getenv(key);
    if (!v) return std::string(def);
    return std::string(v);
}

// --- ROS2 Node Wrapping ---
class FairinoROS2Driver : public rclcpp::Node {
public:
    FairinoROS2Driver(const std::string& node_name)
    : Node(node_name)
    {
        // Setup publishers and subscribers as needed
        joint_move_pub_ = this->create_publisher<sensor_msgs::msg::JointState>("fairino/joint_move", 10);
        ctrl_pub_ = this->create_publisher<std_msgs::msg::String>("fairino/control", 10);
        pose_pub_ = this->create_publisher<geometry_msgs::msg::Pose>("fairino/pose_set", 10);
        plan_pub_ = this->create_publisher<std_msgs::msg::String>("fairino/motion_plan", 10);
        ee_pub_ = this->create_publisher<std_msgs::msg::String>("fairino/ee_cmd", 10);

        // Subscriptions for status
        joint_state_sub_ = this->create_subscription<sensor_msgs::msg::JointState>(
            "fairino/joint_states", 10,
            [this](sensor_msgs::msg::JointState::SharedPtr msg) {
                std::lock_guard<std::mutex> lock(this->status_mutex_);
                last_joint_state_ = *msg;
            });

        pose_sub_ = this->create_subscription<geometry_msgs::msg::Pose>(
            "fairino/robot_pose", 10,
            [this](geometry_msgs::msg::Pose::SharedPtr msg) {
                std::lock_guard<std::mutex> lock(this->status_mutex_);
                last_pose_ = *msg;
            });

        controller_state_sub_ = this->create_subscription<std_msgs::msg::String>(
            "fairino/controller_state", 10,
            [this](std_msgs::msg::String::SharedPtr msg) {
                std::lock_guard<std::mutex> lock(this->status_mutex_);
                last_controller_state_ = msg->data;
            });

        // Add other subscriptions as needed for full status feedback
    }

    // --- Robot commands ---
    void send_joint_move(const std::vector<double>& joints) {
        sensor_msgs::msg::JointState msg;
        msg.position = joints;
        joint_move_pub_->publish(msg);
    }

    void send_ctrl(const std::string& ctrl) {
        std_msgs::msg::String msg;
        msg.data = ctrl;
        ctrl_pub_->publish(msg);
    }

    void send_pose(const std::vector<double>& pose) {
        geometry_msgs::msg::Pose msg;
        if (pose.size() >= 7) {
            msg.position.x = pose[0];
            msg.position.y = pose[1];
            msg.position.z = pose[2];
            msg.orientation.x = pose[3];
            msg.orientation.y = pose[4];
            msg.orientation.z = pose[5];
            msg.orientation.w = pose[6];
        }
        pose_pub_->publish(msg);
    }

    void send_plan(const std::string& plan) {
        std_msgs::msg::String msg;
        msg.data = plan;
        plan_pub_->publish(msg);
    }

    void send_ee_command(const std::string& ee_cmd) {
        std_msgs::msg::String msg;
        msg.data = ee_cmd;
        ee_pub_->publish(msg);
    }

    // --- Status retrieval ---
    std::string get_status_json() {
        std::lock_guard<std::mutex> lock(status_mutex_);
        std::ostringstream oss;
        oss << "{";
        // Joint positions
        oss << "\"joints\":[";
        for (size_t i=0; i<last_joint_state_.position.size(); ++i) {
            if (i>0) oss << ",";
            oss << last_joint_state_.position[i];
        }
        oss << "]";
        // Pose
        oss << ",\"pose\":{";
        oss << "\"x\":" << last_pose_.position.x << ",\"y\":" << last_pose_.position.y << ",\"z\":" << last_pose_.position.z;
        oss << ",\"qx\":" << last_pose_.orientation.x << ",\"qy\":" << last_pose_.orientation.y << ",\"qz\":" << last_pose_.orientation.z << ",\"qw\":" << last_pose_.orientation.w;
        oss << "}";
        // Controller
        oss << ",\"controller_state\":\"" << last_controller_state_ << "\"";
        oss << "}";
        return oss.str();
    }

private:
    // Publishers
    rclcpp::Publisher<sensor_msgs::msg::JointState>::SharedPtr joint_move_pub_;
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr ctrl_pub_;
    rclcpp::Publisher<geometry_msgs::msg::Pose>::SharedPtr pose_pub_;
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr plan_pub_;
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr ee_pub_;

    // Subscriptions
    rclcpp::Subscription<sensor_msgs::msg::JointState>::SharedPtr joint_state_sub_;
    rclcpp::Subscription<geometry_msgs::msg::Pose>::SharedPtr pose_sub_;
    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr controller_state_sub_;

    // Last status (protected by mutex)
    sensor_msgs::msg::JointState last_joint_state_;
    geometry_msgs::msg::Pose last_pose_;
    std::string last_controller_state_;
    std::mutex status_mutex_;
};

// --- HTTP Server and ROS2 Integration ---
int main(int argc, char * argv[]) {
    // Read config from env
    std::string ROS_DOMAIN_ID = getenv_or("ROS_DOMAIN_ID", "0");
    std::string SERVER_HOST = getenv_or("HTTP_SERVER_HOST", "0.0.0.0");
    std::string SERVER_PORT = getenv_or("HTTP_SERVER_PORT", "8080");

    setenv("ROS_DOMAIN_ID", ROS_DOMAIN_ID.c_str(), 1);

    // Start ROS2
    rclcpp::init(argc, argv);
    auto driver_node = std::make_shared<FairinoROS2Driver>("fairino_http_driver");

    // HTTP server
    httplib::Server svr;

    // POST /move
    svr.Post("/move", [driver_node](const httplib::Request& req, httplib::Response& res) {
        // Expect JSON: {"joints":[j1,j2,...,j6]}
        try {
            auto body = req.body;
            size_t pos = body.find("\"joints\"");
            if (pos == std::string::npos) { res.status=400; res.set_content("{\"error\":\"Missing joints\"}", "application/json"); return; }
            std::vector<double> joints;
            pos = body.find("[", pos);
            size_t end = body.find("]", pos);
            std::string v = body.substr(pos+1, end-pos-1);
            std::istringstream ss(v);
            std::string token;
            while (std::getline(ss, token, ',')) {
                joints.push_back(std::stod(token));
            }
            if (joints.size() < 6) { res.status=400; res.set_content("{\"error\":\"At least 6 joints required\"}", "application/json"); return; }
            driver_node->send_joint_move(joints);
            res.set_content("{\"status\":\"ok\"}", "application/json");
        } catch (...) {
            res.status=400;
            res.set_content("{\"error\":\"Parse error\"}", "application/json");
        }
    });

    // PUT /ctrl
    svr.Put("/ctrl", [driver_node](const httplib::Request& req, httplib::Response& res) {
        // Expect JSON: {"controller":"controller_name"}
        try {
            auto body = req.body;
            size_t pos = body.find("\"controller\"");
            if (pos == std::string::npos) { res.status=400; res.set_content("{\"error\":\"Missing controller\"}", "application/json"); return; }
            pos = body.find(":", pos);
            pos = body.find_first_of("\"'", pos);
            size_t end = body.find_first_of("\"'", pos+1);
            std::string ctrl = body.substr(pos+1, end-pos-1);
            driver_node->send_ctrl(ctrl);
            res.set_content("{\"status\":\"ok\"}", "application/json");
        } catch (...) {
            res.status=400;
            res.set_content("{\"error\":\"Parse error\"}", "application/json");
        }
    });

    // PUT /pose
    svr.Put("/pose", [driver_node](const httplib::Request& req, httplib::Response& res) {
        // Expect JSON: {"pose":[x,y,z,qx,qy,qz,qw]}
        try {
            auto body = req.body;
            size_t pos = body.find("\"pose\"");
            if (pos == std::string::npos) { res.status=400; res.set_content("{\"error\":\"Missing pose\"}", "application/json"); return; }
            std::vector<double> pose;
            pos = body.find("[", pos);
            size_t end = body.find("]", pos);
            std::string v = body.substr(pos+1, end-pos-1);
            std::istringstream ss(v);
            std::string token;
            while (std::getline(ss, token, ',')) {
                pose.push_back(std::stod(token));
            }
            if (pose.size() < 7) { res.status=400; res.set_content("{\"error\":\"7 values required\"}", "application/json"); return; }
            driver_node->send_pose(pose);
            res.set_content("{\"status\":\"ok\"}", "application/json");
        } catch (...) {
            res.status=400;
            res.set_content("{\"error\":\"Parse error\"}", "application/json");
        }
    });

    // POST /plan
    svr.Post("/plan", [driver_node](const httplib::Request& req, httplib::Response& res) {
        // Expect JSON: {"plan":"plan_data"}
        try {
            auto body = req.body;
            size_t pos = body.find("\"plan\"");
            if (pos == std::string::npos) { res.status=400; res.set_content("{\"error\":\"Missing plan\"}", "application/json"); return; }
            pos = body.find(":", pos);
            pos = body.find_first_of("\"'", pos);
            size_t end = body.find_first_of("\"'", pos+1);
            std::string plan = body.substr(pos+1, end-pos-1);
            driver_node->send_plan(plan);
            res.set_content("{\"status\":\"ok\"}", "application/json");
        } catch (...) {
            res.status=400;
            res.set_content("{\"error\":\"Parse error\"}", "application/json");
        }
    });

    // POST /ee
    svr.Post("/ee", [driver_node](const httplib::Request& req, httplib::Response& res) {
        // Expect JSON: {"ee_cmd":"open"} or similar
        try {
            auto body = req.body;
            size_t pos = body.find("\"ee_cmd\"");
            if (pos == std::string::npos) { res.status=400; res.set_content("{\"error\":\"Missing ee_cmd\"}", "application/json"); return; }
            pos = body.find(":", pos);
            pos = body.find_first_of("\"'", pos);
            size_t end = body.find_first_of("\"'", pos+1);
            std::string ee_cmd = body.substr(pos+1, end-pos-1);
            driver_node->send_ee_command(ee_cmd);
            res.set_content("{\"status\":\"ok\"}", "application/json");
        } catch (...) {
            res.status=400;
            res.set_content("{\"error\":\"Parse error\"}", "application/json");
        }
    });

    // GET /status
    svr.Get("/status", [driver_node](const httplib::Request& req, httplib::Response& res) {
        res.set_content(driver_node->get_status_json(), "application/json");
    });

    // ROS2 spin in separate thread
    std::thread ros_thread([&](){
        rclcpp::spin(driver_node);
    });

    std::cout << "Fairino HTTP ROS2 driver running at " << SERVER_HOST << ":" << SERVER_PORT << std::endl;
    svr.listen(SERVER_HOST.c_str(), std::stoi(SERVER_PORT));
    rclcpp::shutdown();
    ros_thread.join();
    return 0;
}