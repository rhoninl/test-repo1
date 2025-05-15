import os
import asyncio
import json
from typing import List, Dict, Any
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

import rclpy
from rclpy.node import Node

from std_srvs.srv import SetBool
from sensor_msgs.msg import JointState
from fairino_msgs.srv import MoveJoints

# --- Environment Variables ---
ROS_DOMAIN_ID = int(os.getenv("ROS_DOMAIN_ID", "0"))
ROS_NAMESPACE = os.getenv("ROS_NAMESPACE", "")
ROBOT_NAME = os.getenv("ROBOT_NAME", "fairino")
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8080"))

# --- ROS2 Node Helper ---
class RobotROS2Bridge(Node):
    def __init__(self):
        super().__init__("fairino_http_bridge")
        self.joint_states: Dict[str, Any] = {}
        self.status: Dict[str, Any] = {}
        self._joint_state_sub = self.create_subscription(
            JointState,
            f"/{ROBOT_NAME}/joint_states",
            self._joint_states_callback,
            10,
        )
        self._move_client = self.create_client(
            MoveJoints, f"/{ROBOT_NAME}/move_joints"
        )
        self._power_client = self.create_client(
            SetBool, f"/{ROBOT_NAME}/enable"
        )

    def _joint_states_callback(self, msg: JointState):
        self.joint_states = {
            "names": list(msg.name),
            "positions": list(msg.position),
            "velocities": list(msg.velocity),
            "efforts": list(msg.effort),
            "stamp": msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9,
        }

    async def get_state(self) -> Dict[str, Any]:
        # Wait for at least one message
        count = 0
        while not self.joint_states and count < 50:
            await asyncio.sleep(0.02)
            count += 1
        if not self.joint_states:
            raise RuntimeError("No joint state data available yet.")
        return self.joint_states

    async def move_joints(
        self,
        positions: List[float],
        velocities: List[float] = None,
        duration: float = 1.0,
    ) -> Dict[str, Any]:
        # Wait for service
        if not self._move_client.wait_for_service(timeout_sec=2.0):
            raise RuntimeError("MoveJoints service unavailable.")
        req = MoveJoints.Request()
        req.positions = positions
        if velocities:
            req.velocities = velocities
        req.duration = float(duration)
        future = self._move_client.call_async(req)
        while not future.done():
            await asyncio.sleep(0.01)
        resp = future.result()
        return {"success": resp.success, "message": resp.message}

    async def power(self, enable: bool) -> Dict[str, Any]:
        if not self._power_client.wait_for_service(timeout_sec=2.0):
            raise RuntimeError("Power service unavailable.")
        req = SetBool.Request()
        req.data = enable
        future = self._power_client.call_async(req)
        while not future.done():
            await asyncio.sleep(0.01)
        resp = future.result()
        return {"success": resp.success, "message": resp.message}

# --- Async ROS2 Event Loop Management ---
class ROS2AsyncioBridge:
    def __init__(self):
        rclpy.init(args=None)
        self.node = RobotROS2Bridge()
        self._spin_task = None

    async def start(self):
        loop = asyncio.get_event_loop()
        self._spin_task = loop.create_task(self._spin())

    async def _spin(self):
        while rclpy.ok():
            rclpy.spin_once(self.node, timeout_sec=0.01)
            await asyncio.sleep(0.005)

    async def shutdown(self):
        if self._spin_task:
            self._spin_task.cancel()
        self.node.destroy_node()
        rclpy.shutdown()

ros2_bridge = ROS2AsyncioBridge()

# --- FastAPI App ---
app = FastAPI()

@app.on_event("startup")
async def startup_event():
    await ros2_bridge.start()

@app.on_event("shutdown")
async def shutdown_event():
    await ros2_bridge.shutdown()

@app.get("/state")
async def get_state():
    try:
        state = await ros2_bridge.node.get_state()
        return JSONResponse(state)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"State unavailable: {e}")

@app.post("/move")
async def move_robot(request: Request):
    try:
        body = await request.json()
        positions = body.get("positions")
        velocities = body.get("velocities", None)
        duration = body.get("duration", 1.0)
        if not isinstance(positions, list) or not all(isinstance(p, (int, float)) for p in positions):
            raise HTTPException(status_code=400, detail="positions must be a list of numbers")
        res = await ros2_bridge.node.move_joints(positions, velocities, duration)
        return JSONResponse(res)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Move failed: {e}")

@app.post("/power")
async def power_robot(request: Request):
    try:
        body = await request.json()
        enable = body.get("enable", None)
        if not isinstance(enable, bool):
            raise HTTPException(status_code=400, detail="enable must be a boolean")
        res = await ros2_bridge.node.power(enable)
        return JSONResponse(res)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Power command failed: {e}")

if __name__ == "__main__":
    uvicorn.run("main:app", host=SERVER_HOST, port=SERVER_PORT, reload=False)
