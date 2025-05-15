package main

import (
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/joho/godotenv"
	rclgo "github.com/tiiuae/rclgo"
	"github.com/tiiuae/rclgo/ros2/types/std_msgs"
)

// RobotStatus represents the real-time data structure to be served on /status.
type RobotStatus struct {
	JointPositions      []float64 `json:"joint_positions"`
	JointFeedback       []float64 `json:"joint_feedback"`
	RobotPose           []float64 `json:"robot_pose"`
	EndEffectorState    string    `json:"end_effector_state"`
	PlanningGroups      []string  `json:"planning_groups"`
	RealTimeFeedback    string    `json:"real_time_feedback"`
	SynchronizationStatus string  `json:"synchronization_status"`
}

// CommandRequest represents the structure of incoming /cmd requests.
type CommandRequest struct {
	CommandType string                 `json:"command"`
	Parameters  map[string]interface{} `json:"parameters"`
}

// CommandResponse represents the structure of /cmd responses.
type CommandResponse struct {
	Status  string      `json:"status"`
	Message string      `json:"message"`
	Result  interface{} `json:"result,omitempty"`
}

// EnvConfig holds configuration loaded from environment variables.
type EnvConfig struct {
	RobotIP    string
	RosDomain  string
	ServerHost string
	ServerPort int
}

func loadConfig() EnvConfig {
	_ = godotenv.Load() // Load .env if present, ignore error if not.
	port, err := strconv.Atoi(os.Getenv("SERVER_PORT"))
	if err != nil || port < 1 {
		port = 8080
	}
	return EnvConfig{
		RobotIP:    os.Getenv("ROBOT_IP"),
		RosDomain:  os.Getenv("ROS_DOMAIN_ID"),
		ServerHost: os.Getenv("SERVER_HOST"),
		ServerPort: port,
	}
}

// ROS2 Node and subscriptions
type RobotROS2 struct {
	node   *rclgo.Node
	status RobotStatus
}

func NewRobotROS2(ctx context.Context, domainID string) (*RobotROS2, error) {
	rclgo.SetEnv(map[string]string{"ROS_DOMAIN_ID": domainID})

	node, err := rclgo.NewNode(ctx, "fairino_driver_node", "")
	if err != nil {
		return nil, err
	}
	robot := &RobotROS2{node: node}
	robot.subscribeTopics()
	return robot, nil
}

func (r *RobotROS2) subscribeTopics() {
	// These topics are illustrative. Replace with actual topic names/types as provided by Fairino.
	// Example: /joint_states, /robot_pose, /end_effector/state, /planning_groups, etc.

	// Joint positions/feedback
	r.node.NewSubscription("/joint_positions", std_msgs.MsgFloat64MultiArrayTypeSupport, func(msg *rclgo.Msg) {
		if arr, ok := msg.Data().([]float64); ok {
			r.status.JointPositions = arr
		}
	})
	r.node.NewSubscription("/joint_feedback", std_msgs.MsgFloat64MultiArrayTypeSupport, func(msg *rclgo.Msg) {
		if arr, ok := msg.Data().([]float64); ok {
			r.status.JointFeedback = arr
		}
	})

	// Robot pose
	r.node.NewSubscription("/robot_pose", std_msgs.MsgFloat64MultiArrayTypeSupport, func(msg *rclgo.Msg) {
		if arr, ok := msg.Data().([]float64); ok {
			r.status.RobotPose = arr
		}
	})

	// End effector state
	r.node.NewSubscription("/end_effector/state", std_msgs.MsgStringTypeSupport, func(msg *rclgo.Msg) {
		r.status.EndEffectorState = msg.(*std_msgs.MsgString).Data
	})

	// Planning groups
	r.node.NewSubscription("/planning_groups", std_msgs.MsgStringTypeSupport, func(msg *rclgo.Msg) {
		// Assume planning groups are comma-separated strings.
		r.status.PlanningGroups = append([]string{}, msg.(*std_msgs.MsgString).Data)
	})

	// Real-time feedback
	r.node.NewSubscription("/real_time_feedback", std_msgs.MsgStringTypeSupport, func(msg *rclgo.Msg) {
		r.status.RealTimeFeedback = msg.(*std_msgs.MsgString).Data
	})

	// Synchronization status
	r.node.NewSubscription("/sync_status", std_msgs.MsgStringTypeSupport, func(msg *rclgo.Msg) {
		r.status.SynchronizationStatus = msg.(*std_msgs.MsgString).Data
	})
}

func (r *RobotROS2) SendCommand(ctx context.Context, cmd CommandRequest) (CommandResponse, error) {
	// Map command types to topics/messages as needed. Actual implementation depends on Fairino's ROS2 API.
	switch cmd.CommandType {
	case "move":
		// Publish move command
		pose, ok := cmd.Parameters["pose"].([]float64)
		if !ok {
			return CommandResponse{Status: "error", Message: "Missing or invalid 'pose' parameter"}, nil
		}
		pub, err := r.node.NewPublisher("/move_to_pose", std_msgs.MsgFloat64MultiArrayTypeSupport)
		if err != nil {
			return CommandResponse{Status: "error", Message: "Failed to create publisher"}, nil
		}
		defer pub.Close()
		msg := std_msgs.NewMsgFloat64MultiArray()
		msg.Data = pose
		pub.Publish(msg)
		return CommandResponse{Status: "success", Message: "Move command sent", Result: nil}, nil

	case "plan":
		// Publish plan command
		trajectory, ok := cmd.Parameters["trajectory"].([]float64)
		if !ok {
			return CommandResponse{Status: "error", Message: "Missing or invalid 'trajectory' parameter"}, nil
		}
		pub, err := r.node.NewPublisher("/plan_trajectory", std_msgs.MsgFloat64MultiArrayTypeSupport)
		if err != nil {
			return CommandResponse{Status: "error", Message: "Failed to create publisher"}, nil
		}
		defer pub.Close()
		msg := std_msgs.NewMsgFloat64MultiArray()
		msg.Data = trajectory
		pub.Publish(msg)
		return CommandResponse{Status: "success", Message: "Plan command sent", Result: nil}, nil

	case "activate":
		pub, err := r.node.NewPublisher("/activate_controller", std_msgs.MsgBoolTypeSupport)
		if err != nil {
			return CommandResponse{Status: "error", Message: "Failed to create publisher"}, nil
		}
		defer pub.Close()
		msg := std_msgs.NewMsgBool()
		msg.Data = true
		pub.Publish(msg)
		return CommandResponse{Status: "success", Message: "Activate command sent", Result: nil}, nil

	case "demo":
		pub, err := r.node.NewPublisher("/launch_demo", std_msgs.MsgEmptyTypeSupport)
		if err != nil {
			return CommandResponse{Status: "error", Message: "Failed to create publisher"}, nil
		}
		defer pub.Close()
		msg := std_msgs.NewMsgEmpty()
		pub.Publish(msg)
		return CommandResponse{Status: "success", Message: "Demo command sent", Result: nil}, nil

	case "run":
		appName, ok := cmd.Parameters["application"].(string)
		if !ok {
			return CommandResponse{Status: "error", Message: "Missing or invalid 'application' parameter"}, nil
		}
		pub, err := r.node.NewPublisher("/run_motion_application", std_msgs.MsgStringTypeSupport)
		if err != nil {
			return CommandResponse{Status: "error", Message: "Failed to create publisher"}, nil
		}
		defer pub.Close()
		msg := std_msgs.NewMsgString()
		msg.Data = appName
		pub.Publish(msg)
		return CommandResponse{Status: "success", Message: "Run command sent", Result: nil}, nil

	default:
		return CommandResponse{Status: "error", Message: "Unknown command type"}, nil
	}
}

func main() {
	cfg := loadConfig()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	robot, err := NewRobotROS2(ctx, cfg.RosDomain)
	if err != nil {
		log.Fatalf("Failed to initialize ROS 2 node: %v", err)
	}
	defer robot.node.Close()

	// Start ROS2 spinning in a goroutine
	go func() {
		for {
			robot.node.SpinOnce()
			time.Sleep(10 * time.Millisecond)
		}
	}()

	// HTTP Handlers

	http.HandleFunc("/status", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(robot.status)
	})

	http.HandleFunc("/cmd", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, "Failed to read body", http.StatusBadRequest)
			return
		}
		var cmd CommandRequest
		if err := json.Unmarshal(body, &cmd); err != nil {
			http.Error(w, "Invalid JSON", http.StatusBadRequest)
			return
		}
		resp, err := robot.SendCommand(r.Context(), cmd)
		if err != nil {
			http.Error(w, "Failed to send command", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
	})

	addr := cfg.ServerHost + ":" + strconv.Itoa(cfg.ServerPort)
	log.Printf("Fairino Robot HTTP driver listening on %s", addr)
	log.Fatal(http.ListenAndServe(addr, nil))
}