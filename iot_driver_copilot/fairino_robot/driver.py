import os
import asyncio
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from io import BytesIO

import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from sensor_msgs.msg import JointState

# Environment Variables
ROBOT_HOST = os.environ.get('ROBOT_HOST', 'localhost')
ROBOT_ROS2_DOMAIN_ID = int(os.environ.get('ROBOT_ROS2_DOMAIN_ID', '0'))
HTTP_SERVER_HOST = os.environ.get('HTTP_SERVER_HOST', '0.0.0.0')
HTTP_SERVER_PORT = int(os.environ.get('HTTP_SERVER_PORT', '8080'))

# ROS2 Node Implementation
class FairinoROS2Node(Node):
    def __init__(self):
        super().__init__('fairino_http_bridge')
        self.latest_joint_state = None
        self.latest_robot_state = None
        self.latest_robot_pose = None
        
        # Subscriptions
        self.create_subscription(JointState, '/joint_states', self.joint_state_callback, 10)
        self.create_subscription(String, '/robot_state', self.robot_state_callback, 10)
        self.create_subscription(String, '/robot_pose', self.robot_pose_callback, 10)
        
        # Publishers
        self.grasp_pub = self.create_publisher(String, '/grasp_command', 10)
        self.move_pub = self.create_publisher(String, '/move_command', 10)
    
    def joint_state_callback(self, msg):
        self.latest_joint_state = msg
    
    def robot_state_callback(self, msg):
        self.latest_robot_state = msg.data
    
    def robot_pose_callback(self, msg):
        self.latest_robot_pose = msg.data
    
    def publish_grasp(self, command):
        msg = String()
        msg.data = command
        self.grasp_pub.publish(msg)
    
    def publish_move(self, command):
        msg = String()
        msg.data = command
        self.move_pub.publish(msg)
    
    def get_state(self):
        # Compose robot state data
        return {
            "joint_positions": {
                "names": self.latest_joint_state.name if self.latest_joint_state else [],
                "positions": self.latest_joint_state.position if self.latest_joint_state else [],
                "velocities": self.latest_joint_state.velocity if self.latest_joint_state else [],
                "effort": self.latest_joint_state.effort if self.latest_joint_state else []
            },
            "robot_state": json.loads(self.latest_robot_state) if self.latest_robot_state else {},
            "robot_pose": json.loads(self.latest_robot_pose) if self.latest_robot_pose else {}
        }


# HTTP Server Implementation
class FairinoHTTPRequestHandler(BaseHTTPRequestHandler):
    ros2_node = None

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/state':
            # Gather state from ROS2 node
            state = self.ros2_node.get_state()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(state).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not Found')

    def do_POST(self):
        parsed = urlparse(self.path)
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body.decode('utf-8')) if body else {}
        except Exception:
            data = {}

        if parsed.path == '/grasp':
            # Accept grasp command and publish to ROS2 topic
            command = data.get('command', '')
            if command:
                self.ros2_node.publish_grasp(command)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok", "msg": "Grasp command sent"}).encode('utf-8'))
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'Missing command')
        elif parsed.path == '/move':
            # Accept move command and publish to ROS2 topic
            command = data.get('command', '')
            if command:
                self.ros2_node.publish_move(command)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok", "msg": "Move command sent"}).encode('utf-8'))
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'Missing command')
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not Found')

def run_http_server(ros2_node):
    FairinoHTTPRequestHandler.ros2_node = ros2_node
    server = HTTPServer((HTTP_SERVER_HOST, HTTP_SERVER_PORT), FairinoHTTPRequestHandler)
    print(f'Fairino HTTP Driver listening on {HTTP_SERVER_HOST}:{HTTP_SERVER_PORT}')
    server.serve_forever()

def main():
    os.environ['ROS_DOMAIN_ID'] = str(ROBOT_ROS2_DOMAIN_ID)
    rclpy.init()
    ros2_node = FairinoROS2Node()
    try:
        import threading
        t = threading.Thread(target=run_http_server, args=(ros2_node,), daemon=True)
        t.start()
        rclpy.spin(ros2_node)
    except KeyboardInterrupt:
        pass
    finally:
        ros2_node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()