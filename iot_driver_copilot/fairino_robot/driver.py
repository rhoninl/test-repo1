import os
import asyncio
import json
from typing import List, Dict
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

import rclpy
from rclpy.node import Node
from rclpy.executors import SingleThreadedExecutor
from std_msgs.msg import Bool
from sensor_msgs.msg import JointState
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint

# Environment variables for configuration
ROBOT_NAMESPACE = os.environ.get("ROBOT_NAMESPACE", "")
ROS_DOMAIN_ID = os.environ.get("ROS_DOMAIN_ID", "0")
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8080"))
ROS2_STATE_TOPIC = os.environ.get("ROS2_STATE_TOPIC", "joint_states")
ROS2_POWER_TOPIC = os.environ.get("ROS2_POWER_TOPIC", "robot_enable")
ROS2_TRAJECTORY_TOPIC = os.environ.get("ROS2_TRAJECTORY_TOPIC", "joint_trajectory")

os.environ["ROS_DOMAIN_ID"] = ROS_DOMAIN_ID

# Shared state for joint data
class RobotState:
    def __init__(self):
        self.joint_names: List[str] = []
        self.positions: List[float] = []
        self.velocities: List[float] = []
        self.efforts: List[float] = []
        self.last_update: float = 0.0

    def update(self, msg: JointState):
        self.joint_names = msg.name
        self.positions = msg.position
        self.velocities = msg.velocity
        self.efforts = msg.effort
        self.last_update = rclpy.clock.Clock().now().nanoseconds / 1e9

    def to_dict(self):
        return {
            "joint_names": self.joint_names,
            "positions": self.positions,
            "velocities": self.velocities,
            "efforts": self.efforts,
            "last_update": self.last_update
        }

robot_state = RobotState()

# ROS2 node for integration
class FairinoRos2Bridge(Node):
    def __init__(self):
        super().__init__("fairino_driver_bridge")
        self.state_sub = self.create_subscription(
            JointState,
            ROS2_STATE_TOPIC,
            self.joint_state_callback,
            5
        )
        self.power_pub = self.create_publisher(
            Bool,
            ROS2_POWER_TOPIC,
            5
        )
        self.trajectory_pub = self.create_publisher(
            JointTrajectory,
            ROS2_TRAJECTORY_TOPIC,
            5
        )

    def joint_state_callback(self, msg: JointState):
        robot_state.update(msg)

    def publish_power(self, enable: bool):
        msg = Bool()
        msg.data = enable
        self.power_pub.publish(msg)

    def publish_trajectory(self, points: List[Dict]):
        jt = JointTrajectory()
        jt.header.stamp = self.get_clock().now().to_msg()
        jt.joint_names = robot_state.joint_names or [f"j{i+1}" for i in range(6)]  # fallback
        jt.points = []
        for pt in points:
            jp = JointTrajectoryPoint()
            jp.positions = pt.get("positions", [])
            jp.velocities = pt.get("velocities", [])
            jp.time_from_start.sec = int(pt.get("time_from_start", 1))
            jt.points.append(jp)
        self.trajectory_pub.publish(jt)

# ROS2 node & executor in background
class ROS2Background:
    def __init__(self):
        self.loop = asyncio.get_event_loop()
        rclpy.init(args=None)
        self.node = FairinoRos2Bridge()
        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.node)
        self.task = self.loop.create_task(self.spin())

    async def spin(self):
        while rclpy.ok():
            rclpy.spin_once(self.node, timeout_sec=0.1)
            await asyncio.sleep(0.05)

    def publish_power(self, enable: bool):
        self.node.publish_power(enable)

    def publish_trajectory(self, points: List[Dict]):
        self.node.publish_trajectory(points)

ros2_bg = None

# FastAPI setup
app = FastAPI()

@app.on_event("startup")
async def startup_event():
    global ros2_bg
    ros2_bg = ROS2Background()

@app.on_event("shutdown")
async def shutdown_event():
    if ros2_bg:
        rclpy.shutdown()

@app.get("/state")
async def get_state():
    return JSONResponse(content=robot_state.to_dict())

@app.post("/power")
async def post_power(request: Request):
    data = await request.json()
    if "enable" not in data or not isinstance(data["enable"], bool):
        raise HTTPException(status_code=400, detail="Payload must include boolean field 'enable'")
    ros2_bg.publish_power(data["enable"])
    return JSONResponse(content={"success": True, "enabled": data["enable"]})

@app.post("/move")
async def post_move(request: Request):
    data = await request.json()
    points = data.get("points")
    if not isinstance(points, list) or not points:
        raise HTTPException(status_code=400, detail="Payload must contain 'points' as a list")
    ros2_bg.publish_trajectory(points)
    return JSONResponse(content={"success": True, "points_sent": len(points)})

if __name__ == "__main__":
    uvicorn.run("main:app", host=SERVER_HOST, port=SERVER_PORT, reload=False)
