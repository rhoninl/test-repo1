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
	"github.com/robfig/cron/v3"
	"gopkg.in/yaml.v2"
	// ROS 2 Go client import assumes use of github.com/sequenceplanner/go-ros2
	"github.com/sequenceplanner/go-ros2/ros2"
	"github.com/sequenceplanner/go-ros2/ros2/types"
)

// --- Environment Variables ---
var (
	robotIP        string
	ros2DomainID   string
	ros2NodeName   string
	serverHost     string
	serverPort     string
	ros2StatusTopic string
	ros2CmdTopic    string
	ros2QoS         int
)

// --- Data Structures ---

type StatusResponse struct {
	Timestamp           time.Time              `json:"timestamp"`
	JointPositions      map[string]float64     `json:"joint_positions"`
	JointFeedback       map[string]float64     `json:"joint_feedback"`
	RobotPoses          map[string]interface{} `json:"robot_poses"`
	EndEffectorState    map[string]interface{} `json:"end_effector_state"`
	PlanningGroups      []string               `json:"planning_groups"`
	SyncStatus          string                 `json:"synchronization_status"`
	RawYAML             string                 `json:"yaml,omitempty"`
	RawXML              string                 `json:"xml,omitempty"`
}

type CommandRequest struct {
	Command   string                 `json:"command"` // e.g., move, plan, activate, demo, run
	Params    map[string]interface{} `json:"params"`
}

type CommandResult struct {
	Success   bool        `json:"success"`
	Message   string      `json:"message"`
	Data      interface{} `json:"data,omitempty"`
}

// --- ROS 2 Communication Helpers ---

type FairinoROS2Client struct {
	node     *ros2.Node
	statusSub *ros2.Subscription
	cmdPub    *ros2.Publisher
}

func newFairinoROS2Client(ctx context.Context) (*FairinoROS2Client, error) {
	node, err := ros2.NewNode(ros2NodeName, "")
	if err != nil {
		return nil, err
	}

	statusSub, err := node.NewSubscription(
		ros2StatusTopic,
		"fairino_msgs/msg/RobotStatus",
		uint8(ros2QoS),
	)
	if err != nil {
		return nil, err
	}

	cmdPub, err := node.NewPublisher(
		ros2CmdTopic,
		"fairino_msgs/msg/RobotCommand",
		uint8(ros2QoS),
	)
	if err != nil {
		return nil, err
	}

	return &FairinoROS2Client{
		node:     node,
		statusSub: statusSub,
		cmdPub:    cmdPub,
	}, nil
}

func (c *FairinoROS2Client) GetStatus(ctx context.Context) (*StatusResponse, error) {
	// Wait for status message with timeout
	type msgResult struct {
		msg types.Message
		ok  bool
	}
	ch := make(chan msgResult, 1)
	go func() {
		msg, ok := c.statusSub.TakeMessage(ctx)
		ch <- msgResult{msg, ok}
	}()
	select {
	case r := <-ch:
		if !r.ok {
			return nil, io.EOF
		}
		// Extract fields from the ROS 2 message (structure must match fairino_msgs/msg/RobotStatus)
		// Dummy extraction since actual type is unknown
		// In production, use generated types or dynamic introspection
		status := StatusResponse{
			Timestamp:        time.Now(),
			JointPositions:   map[string]float64{"joint1": 0.0}, // Mocked values, replace with real ones
			JointFeedback:    map[string]float64{},
			RobotPoses:       map[string]interface{}{},
			EndEffectorState: map[string]interface{}{},
			PlanningGroups:   []string{"group1"},
			SyncStatus:       "synced",
		}
		// Optionally convert to YAML/XML
		yamlRaw, _ := yaml.Marshal(status)
		status.RawYAML = string(yamlRaw)
		// Not generating XML here (could use encoding/xml if needed)
		return &status, nil
	case <-time.After(2 * time.Second):
		return nil, context.DeadlineExceeded
	}
}

func (c *FairinoROS2Client) SendCommand(ctx context.Context, cmd CommandRequest) (*CommandResult, error) {
	// Build ROS 2 command message (structure must match fairino_msgs/msg/RobotCommand)
	msg := types.NewDynamicMessage("fairino_msgs/msg/RobotCommand")
	_ = msg.Set("command_type", cmd.Command)
	_ = msg.Set("params", cmd.Params)
	if err := c.cmdPub.Publish(msg); err != nil {
		return &CommandResult{
			Success: false,
			Message: "Failed to publish command: " + err.Error(),
		}, err
	}
	return &CommandResult{
		Success: true,
		Message: "Command sent",
	}, nil
}

// --- HTTP Handlers ---

func handleStatus(rosClient *FairinoROS2Client) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
		defer cancel()
		status, err := rosClient.GetStatus(ctx)
		if err != nil {
			http.Error(w, "Failed to retrieve robot status: "+err.Error(), http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(status)
	}
}

func handleCmd(rosClient *FairinoROS2Client) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var cmd CommandRequest
		if err := json.NewDecoder(r.Body).Decode(&cmd); err != nil {
			http.Error(w, "Invalid command request", http.StatusBadRequest)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
		defer cancel()
		result, err := rosClient.SendCommand(ctx, cmd)
		if err != nil {
			http.Error(w, result.Message, http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(result)
	}
}

// --- Main Entrypoint ---

func mustEnv(key string, fallback string) string {
	v := os.Getenv(key)
	if v != "" {
		return v
	}
	if fallback != "" {
		return fallback
	}
	log.Fatalf("Missing required env: %s", key)
	return ""
}

func mustEnvInt(key string, fallback int) int {
	v := os.Getenv(key)
	if v != "" {
		i, err := strconv.Atoi(v)
		if err == nil {
			return i
		}
	}
	return fallback
}

func main() {
	_ = godotenv.Load()

	robotIP = mustEnv("ROBOT_IP", "")
	ros2DomainID = mustEnv("ROS2_DOMAIN_ID", "0")
	ros2NodeName = mustEnv("ROS2_NODE_NAME", "fairino_driver_node")
	serverHost = mustEnv("SERVER_HOST", "0.0.0.0")
	serverPort = mustEnv("SERVER_PORT", "8080")
	ros2StatusTopic = mustEnv("ROS2_STATUS_TOPIC", "/robot/status")
	ros2CmdTopic = mustEnv("ROS2_CMD_TOPIC", "/robot/cmd")
	ros2QoS = mustEnvInt("ROS2_QOS", 1)

	os.Setenv("ROS_DOMAIN_ID", ros2DomainID)

	ctx := context.Background()
	rosClient, err := newFairinoROS2Client(ctx)
	if err != nil {
		log.Fatalf("Failed to connect to ROS 2: %v", err)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/status", handleStatus(rosClient))
	mux.HandleFunc("/cmd", handleCmd(rosClient))

	srv := &http.Server{
		Addr:    serverHost + ":" + serverPort,
		Handler: mux,
	}

	go func() {
		log.Printf("Fairino Robot HTTP driver started on %s:%s", serverHost, serverPort)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("HTTP server exited: %v", err)
		}
	}()

	// Wait forever
	select {}
}