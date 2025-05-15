import os
import asyncio
import json
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional
import rclpy
from rclpy.node import Node
from rclpy.executors import SingleThreadedExecutor
from std_msgs.msg import Bool
from sensor_msgs.msg import JointState
from fairino_msgs.msg import RobotStatus
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint

# Configuration from environment variables
ROS2_NAMESPACE = os.getenv("ROS2_NAMESPACE", "")
ROS2_JOINT_STATE_TOPIC = os.getenv("ROS2_JOINT_STATE_TOPIC", "/joint_states")
ROS2_STATUS_TOPIC = os.getenv("ROS2_STATUS_TOPIC", "/robot_status")
ROS2_ENABLE_TOPIC = os.getenv("ROS2_ENABLE_TOPIC", "/robot_enable")
ROS2_TRAJ_TOPIC = os.getenv("ROS2_TRAJ_TOPIC", "/joint_trajectory")
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))

# Shared state for robot joint/robot status
class RobotState:
    def __init__(self):
        self.joint_state = None
        self.status = None

robot_state = RobotState()

# ROS2 Background Node
class FairinoRos2Node(Node):
    def __init__(self):
        super().__init__('fairino_http_driver')
        # Subscribers
        self.sub_joint = self.create_subscription(
            JointState, ROS2_JOINT_STATE_TOPIC, self.joint_callback, 10)
        self.sub_status = self.create_subscription(
            RobotStatus, ROS2_STATUS_TOPIC, self.status_callback, 10)
        # Publishers
        self.pub_enable = self.create_publisher(Bool, ROS2_ENABLE_TOPIC, 10)
        self.pub_trajectory = self.create_publisher(JointTrajectory, ROS2_TRAJ_TOPIC, 10)

    def joint_callback(self, msg):
        robot_state.joint_state = msg

    def status_callback(self, msg):
        robot_state.status = msg

    def set_power(self, enable: bool):
        msg = Bool()
        msg.data = enable
        self.pub_enable.publish(msg)

    def move_joints(self, names: List[str], positions: List[float], velocities: Optional[List[float]] = None, effort: Optional[List[float]] = None, time_from_start: float = 1.0):
        traj_msg = JointTrajectory()
        traj_msg.joint_names = names
        point = JointTrajectoryPoint()
        point.positions = positions
        if velocities:
            point.velocities = velocities
        if effort:
            point.effort = effort
        point.time_from_start.sec = int(time_from_start)
        point.time_from_start.nanosec = int((time_from_start - int(time_from_start)) * 1e9)
        traj_msg.points = [point]
        self.pub_trajectory.publish(traj_msg)

# Start ROS2 background node in another thread
def start_ros2_node(loop):
    rclpy.init()
    node = FairinoRos2Node()
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    loop.run_until_complete(asyncio.to_thread(executor.spin))
    rclpy.shutdown()

# FastAPI HTTP server
app = FastAPI()

# Models
class PowerRequest(BaseModel):
    enable: bool

class MoveRequest(BaseModel):
    joint_names: List[str]
    positions: List[float]
    velocities: Optional[List[float]] = None
    effort: Optional[List[float]] = None
    time_from_start: Optional[float] = 1.0

@app.on_event("startup")
async def startup_event():
    loop = asyncio.get_event_loop()
    # Start ROS2 node in background thread
    asyncio.create_task(asyncio.to_thread(start_ros2_node, loop))

@app.post("/move")
async def move_robot(data: MoveRequest):
    if not hasattr(app.state, "ros2_node"):
        app.state.ros2_node = FairinoRos2Node()
    try:
        app.state.ros2_node.move_joints(
            data.joint_names,
            data.positions,
            data.velocities,
            data.effort,
            data.time_from_start or 1.0
        )
        return JSONResponse({"result": "Trajectory command sent"}, status_code=200)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/power")
async def set_power(data: PowerRequest):
    if not hasattr(app.state, "ros2_node"):
        app.state.ros2_node = FairinoRos2Node()
    try:
        app.state.ros2_node.set_power(data.enable)
        return JSONResponse({"result": f"Robot {'enabled' if data.enable else 'disabled'}"}, status_code=200)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/state")
async def get_state():
    joint_msg = robot_state.joint_state
    status_msg = robot_state.status
    if not joint_msg and not status_msg:
        raise HTTPException(status_code=503, detail="No robot state available yet")
    result = {}
    if joint_msg:
        result["joint_names"] = joint_msg.name
        result["positions"] = joint_msg.position
        result["velocities"] = joint_msg.velocity
        result["effort"] = joint_msg.effort
    if status_msg:
        # The actual fields depend on fairino_msgs/RobotStatus
        result["robot_status"] = {
            "mode": getattr(status_msg, 'mode', None),
            "error_code": getattr(status_msg, 'error_code', None),
            "is_enabled": getattr(status_msg, 'is_enabled', None)
            # Add more fields as needed based on fairino_msgs/RobotStatus definition
        }
    return JSONResponse(result)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)
