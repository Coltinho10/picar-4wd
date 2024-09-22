import cv2
import os
import sys
import time
import signal
import numpy as np
import multiprocessing as mp

from a_star import *



MODE = 'picamera'

if MODE=='legacy':
    import picar_4wd as fc
    import utils
    from mapping import scan_dist, map_obj
    from tflite_support.task import core, vision, processor

elif MODE=='picamera':
    # Create a car instance
    from tflite_runtime.interpreter import Interpreter
    from picamera2 import Picamera2
    sys.path.insert(0, '../picarx')
    from picarx import Picarx
    fc = Picarx()
    import picamera_utils
    LABEL_PATH='../picarx/labelmap.txt'

    MAX_ANGLE = 90
    MIN_ANGLE = -90
    STEP_ANGLE = 5
    SERVO_ANGLE_ERROR = -5  # servo is 5 degree to the left when reset to 0 degree
    ANGLE_LIST = list(range(MIN_ANGLE, MAX_ANGLE+STEP_ANGLE, STEP_ANGLE))[-1::-1]
    MAX_MAPPING_DIST = 50

    def scan_dist(direct=0):
        if direct:  # left to right
            angles = iter(ANGLE_LIST)
            #print(ANGLE_LIST)
        else:       # right to left
            angles = iter(ANGLE_LIST[-1::-1])
        #print(list(angles))
        ret = list()
        for a in angles:
            ret.append(fc.get_distance_at(a + SERVO_ANGLE_ERROR))
        assert(len(ret) == len(ANGLE_LIST))
        print(ret)
        fc.get_distance_at(0)
        if direct == 0:
            ret.reverse()  # always return dist from left to right
        
        return ret
        
    def map_obj(dist):
        dist_clip = np.clip(dist, a_min=0, a_max=2**31-1)  # processing sensor data, prune -1 and -2, cap to int32 max
        angles_in_rad = np.array(ANGLE_LIST)*np.pi/180
        obj_pos = dist_clip * np.array([np.sin(angles_in_rad), np.cos(angles_in_rad)])  # car pos is (0, 0)
        obj_xy = np.int32(np.round(obj_pos, 0))
        #mapping = np.zeros((MAX_MAPPING_DIST+1, 2*MAX_MAPPING_DIST+1))  # car pos is (0, MAX_MAPPING_DIST)
        mapping = list()
        for i in range(len(ANGLE_LIST)):
            x = obj_xy[0][i]
            y = obj_xy[1][i]
            if  np.abs(x)<=MAX_MAPPING_DIST and y<=MAX_MAPPING_DIST and not (x ==0 and y == 0):
                mapping.append((x+MAX_MAPPING_DIST, y))
                #mapping[MAX_MAPPING_DIST-y][x+MAX_MAPPING_DIST] = 1
                #print(f"x={x+MAX_MAPPING_DIST}, y={MAX_MAPPING_DIST-y}")
                
        return mapping


# Motor contorl constants
DIRECITONS = ['W', 'N', 'E', 'S']
FORWARD_SPEED = 8
FORWARD_TIME = 0.4
TURN_SPEED = 30
TURN_TIME = 1

# Define model and camera parameters
OBJ_DETECT_MODEL = 'efficientdet_lite0.tflite'
CAMERA_ID = 0
FRAME_WIDTH = 640
FRAME_HEIGHT = 480
NUM_THREADS = 4
ENABLE_EDGETPU = False

# Define A Start grid size
GRID_SIZE = 11


def get_direction_distance(start, end):
    ew = end[0] - start[0]
    ns = end[1] - start[1]
    if ns and ew == 0:
        return ('S', ns) if ns > 0 else ('N', ns)
    elif ew and ns == 0:
        return ('E', ew) if ew > 0 else ('W', ew)
    else:
        raise Exception(f"Unexpected start {start} and end {end}")

def cleanup(signum, frame):
    """Handle Ctrl+C to stop the car and clean up."""
    print("\nCtrl+C detected, stopping the car.")
    fc.stop()
    sys.exit(0)

def update_diagram(diagram):
    """ Map scan result to A star grid """
    dist = np.array(scan_dist(1))
    mapping = map_obj(dist)
    
    walls = list()
    for m in mapping:
        wall = (m[0]//10, m[1]//10)
        if wall in walls:
            continue
        walls.append(wall)

    diagram.walls = walls

def auto_drive(start, goal, car, stop_sign_event, drive_done_event):
    """ Main driving logic """
    diagram = GridWithWeights(GRID_SIZE, GRID_SIZE)
    # To replace with adv mapping
    #diagram.walls = [(1, 7), (1, 8), (2, 7), (2, 8), (3, 7), (3, 8)]
#     diagram.walls = [(3, 2), (3, 3), (3, 4), (3, 5),
#                       (4, 2), (4, 3), (4, 4), (4, 5)]
    update_diagram(diagram)
    
    now_loc = start
    came_from, cost_so_far = a_star_search(diagram, now_loc, goal)
    path = reconstruct_path(came_from, start=now_loc, goal=goal)
    draw_grid(diagram, point_to=came_from, start=now_loc, goal=goal)
    print()
    draw_grid(diagram, path=reconstruct_path(came_from, start=now_loc, goal=goal))
    
    for spot in path[1:]:
        car.move_to(spot)
        now_loc = spot
    
    drive_done_event.set()
    print('Auto drive complete')

def object_detection_legacy(stop_sign_event, drive_done_event, flip_frame=False, show_camera=False):
    """Process responsible for object detection."""
    # Initialize camera
    cap = cv2.VideoCapture(CAMERA_ID)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)

    # Visualization parameters
    row_size = 20  # pixels
    left_margin = 24  # pixels
    text_color = (0, 0, 255)  # red
    font_size = 1
    font_thickness = 1
    fps_avg_frame_count = 10

    # Initialize object detection model
    base_options = core.BaseOptions(
        file_name=OBJ_DETECT_MODEL, use_coral=ENABLE_EDGETPU, num_threads=NUM_THREADS)
    detection_options = processor.DetectionOptions(max_results=3, score_threshold=0.3)
    options = vision.ObjectDetectorOptions(
        base_options=base_options, detection_options=detection_options)
    detector = vision.ObjectDetector.create_from_options(options)

    counter, fps = 0, 0
    start_time = time.time()

    # Start object detection loop
    while True:
        if drive_done_event.is_set():
            print("drive_done_event")
            break
        
        success, frame = cap.read()
        if not success:
            sys.exit("ERROR: Unable to read from camera.")

        counter += 1
        
        # Flip the frame if needed
        if flip_frame:
            frame = cv2.rotate(frame, cv2.ROTATE_180)

        """I put this inside this if statement to have it process every other image to help with performance"""
        if counter % 2 == 0:
            # Convert BGR to RGB as required by the model
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

            # Create a TensorImage object from the RGB image.
            input_tensor = vision.TensorImage.create_from_array(rgb_frame)

            # Run object detection estimation
            detection_result = detector.detect(input_tensor)

            # Draw keypoints and edges on input image
            frame = utils.visualize(frame, detection_result)

            # Check if a stop sign is detected
            # ============================================================= 
            # this is the big piece needed for detection, 
            # most of the rest of this script is not new
            # =============================================================
            for detection in detection_result.detections:
                if detection.categories[0].category_name == "stop sign":
                    print("Stop sign detected!")
                    stop_sign_event.set()
            

        # Calculate and display FPS
        if counter % fps_avg_frame_count == 0:
            end_time = time.time()
            fps = fps_avg_frame_count / (end_time - start_time)
            start_time = time.time()

        if show_camera:
            """I saw pretty decent improvement on fps while not displaying the frame livefeed ~4.5fps to 8.5fps"""
            fps_text = 'FPS = {:.1f}'.format(fps)
            #print(fps_text)
            text_location = (left_margin, row_size)
            cv2.putText(frame, fps_text, text_location, cv2.FONT_HERSHEY_PLAIN, font_size, text_color, font_thickness)

            # Display the frame with detection
            cv2.imshow('Object Detection', frame)
            
        # Stop the program if the ESC key is pressed.
        if cv2.waitKey(1) == 27:
            break

    cap.release()
    cv2.destroyAllWindows()


def object_detection_picamera(stop_sign_event, drive_done_event, flip_frame=False, show_camera=False):
    with open(LABEL_PATH, 'r') as f:
        labels = {i: line.strip() for i, line in enumerate(f.readlines())}

    interpreter = Interpreter(model_path=OBJ_DETECT_MODEL, num_threads=NUM_THREADS)
    interpreter.allocate_tensors()
    
    # Get input dimensions from the model
    input_details = interpreter.get_input_details()
    model_height = input_details[0]['shape'][1]
    model_width = input_details[0]['shape'][2]

    picam2 = Picamera2()
    config = picam2.create_preview_configuration(main={"size": (FRAME_WIDTH, FRAME_HEIGHT)})
    picam2.configure(config)
    picam2.start()

    row_size = 20
    left_margin = 24  
    text_color = (0, 0, 255) 
    font_size = 1
    font_thickness = 1
    fps_avg_frame_count = 10

    counter, fps = 0, 0
    start_time = time.time()

    while True:
        if drive_done_event.is_set():
            print("drive_done_event")
            break
        
        frame = picam2.capture_array()
             
        counter += 1

        """I put this inside this if statement to have it process every other image to help with performance"""
        if counter % 2 == 0:
            # Convert BGRA to RGB as required by the model
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB)  

            # Resize the image to the model's expected size
            resized_frame = cv2.resize(frame, (model_width, model_height))

            # Add batch dimension and convert data type
            input_tensor = np.expand_dims(resized_frame, axis=0).astype('uint8')

            # Set the Input Tensor
            interpreter.set_tensor(input_details[0]['index'], input_tensor)

            # Run object detection estimation
            interpreter.invoke()

            # Get Output Details 
            output_details = interpreter.get_output_details()
            
            boxes = interpreter.get_tensor(output_details[0]['index'])[0]  # shape: [1, None, 4]
            classes = interpreter.get_tensor(output_details[1]['index'])[0]  # shape: [1, None]
            scores = interpreter.get_tensor(output_details[2]['index'])[0]  # shape: [1, None]

            detection_result = picamera_utils.create_detections(boxes, classes, scores, labels)
            
            # Draw keypoints and edges on input image
            frame = picamera_utils.visualize(frame, detection_result)

            # Check if a stop sign is detected
            # ============================================================= 
            # this is the big piece needed for detection, 
            # most of the rest of this script is not new
            # =============================================================
            for detection in detection_result:
                if detection.categories[0].category_name == "stop sign":
                    print("Stop sign detected!")
                    stop_sign_event.set()


            # Calculate and display FPS
            if counter % fps_avg_frame_count == 0:
                end_time = time.time()
                fps = fps_avg_frame_count / (end_time - start_time)
                start_time = time.time()        

            fps_text = 'FPS = {:.1f}'.format(fps)
            #print(fps_text)
            
            if show_camera:
                text_location = (left_margin, row_size)
                cv2.putText(frame, fps_text, text_location, cv2.FONT_HERSHEY_PLAIN, font_size, text_color, font_thickness)

                # Display the frame with detection
                cv2.imshow('Object Detection', frame)

        # Stop the program if the ESC key is pressed.
        if cv2.waitKey(1) == 27:
            break
    
    picam2.stop()
    cv2.destroyAllWindows()


class Picar:
    def __init__(self, loc, stop_sign_event):
        self.loc = loc
        self.direction = 'S'
        self.stop_sign_event = stop_sign_event
        
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        fc.stop()
        pass
    
    def __del__(self):
        self.__exit__()
        
    
    def move_to(self, dest):
        if self.stop_sign_event.is_set():
            print('Stop sign, wait for 3 seconds')
            fc.stop()
            time.sleep(3)
        
        print(f"at {self.loc}, moving to {dest}")
        direction, dist = get_direction_distance(self.loc, dest)
        
        self.turn_to(direction)

        print(f"move {dist}")
        fc.forward(FORWARD_SPEED)
        time.sleep(abs(dist)*FORWARD_TIME)
        fc.stop()
        
        if direction in ['W', 'E']:
            self.loc[0] += dist
        else:
            self.loc[1] += dist

        assert(self.loc[0] == dest[0] and self.loc[1] == dest[1])

    def turn_to(self, direction):
        if self.direction == direction: return
        
        cur_dir_idx = DIRECITONS.index(self.direction)

        if DIRECITONS[(cur_dir_idx+1)%4] == direction:
            print('turn right')
            fc.turn_right(TURN_SPEED)
            time.sleep(TURN_TIME)
            fc.stop()
        elif DIRECITONS[cur_dir_idx-1] == direction:
            print('turn left')
            fc.turn_left(TURN_SPEED)
            time.sleep(TURN_TIME)
            fc.stop()
            
        else:
            raise Exception(f"cannot turn from {self.direction} to {direction}")

        self.direction = direction
    

if __name__ == "__main__":

    start, goal = (GRID_SIZE//2, 0), (GRID_SIZE-1, GRID_SIZE-1)

    signal.signal(signal.SIGINT, cleanup)

    # Create multiprocessing Events to
    stop_sign_event = mp.Event()  # object_detection_process signals stop sign to auto_drive_process
    drive_done_event = mp.Event()  # auto_drive_procees signals done driving to object_detection_process

    with Picar(list(start), stop_sign_event) as car:
        #car.init_obj_detection()
        
        # Create the processes
        if MODE == 'legacy':
            object_detection_process = mp.Process(target=object_detection_legacy, args=(stop_sign_event, drive_done_event, False, False))
        elif MODE == 'picamera':
            object_detection_process = mp.Process(target=object_detection_picamera, args=(stop_sign_event, drive_done_event, False, False))
        else:
            raise Exception(f"Error: {MODE} mode is not supported")
        auto_drive_process = mp.Process(target=auto_drive, args=(start, goal, car, stop_sign_event, drive_done_event))
        
        # Start the processes
        object_detection_process.start()
        auto_drive_process.start()
        
        # Wait for both processes to complete
        object_detection_process.join()
        auto_drive_process.join()
