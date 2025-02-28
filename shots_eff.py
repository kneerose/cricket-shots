import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

import pandas as pd
import numpy as np
import mediapipe as mp
import pickle
import re
import sys
import cv2
import matplotlib.pyplot as plt
from PIL import Image

from tflite_model_maker.config import ExportFormat, QuantizationConfig
from tflite_model_maker import model_spec
from tflite_model_maker import object_detector
from tflite_support import metadata
import tensorflow as tf

from typing import List, NamedTuple
import json
import cv2
import imutils

from warnings import filterwarnings
filterwarnings('ignore')

# Interface for running tflite models
Interpreter = tf.lite.Interpreter


classes_list = ['0.Cut Shot', '1.Cover Drive', '2.Straight Drive',
                '3.Pull Shot', '4.Leg Glance Shot', '5.Scoop Shot']

idx_features = [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 34, 36, 38, 40, 42, 44, 46, 48, 50, 53, 55, 56, 57, 58,
                59, 61, 63, 65, 66, 67, 68, 69, 73, 74, 75, 77, 81, 82, 83, 85, 89, 90, 91, 92, 94, 96, 98, 103, 104, 106, 107, 112, 115, 119, 120, 128]

pkl_filename = 'model/shot_classification.pkl'
with open(pkl_filename, 'rb') as file:
    model = pickle.load(file)

def predict_shot(imgRGB):

    mpPose = mp.solutions.pose
    pose = mpPose.Pose()
    mpDraw = mp.solutions.drawing_utils  # For drawing keypoints
    points = mpPose.PoseLandmark  # Landmarks

    data = []

    results = pose.process(imgRGB)

    # Run this only when landmarks are detected
    if results.pose_landmarks:
        mpDraw.draw_landmarks(imgRGB, results.pose_landmarks, mpPose.POSE_CONNECTIONS,
                              mpDraw.DrawingSpec(
                                  color=(245, 117, 66), thickness=2, circle_radius=2),
                              mpDraw.DrawingSpec(color=(245, 66, 230), thickness=2, circle_radius=2))
        landmarks = results.pose_landmarks.landmark
        for i, j in zip(points, landmarks):
            data = data + [j.x, j.y, j.z, j.visibility]
    data = [data[i] for i in idx_features]
    result = int(model.predict([data])[0])
    
    # Remove the text class number
    text = f"{re.sub('[^a-zA-Z]', ' ', classes_list[result])}"
    return text, imgRGB

class ObjectDetectorOptions(NamedTuple):
    """A config to initialize an object detector."""

    label_allow_list: List[str] = None
    """The optional allow list of labels."""

    label_deny_list: List[str] = None
    """The optional deny list of labels."""

    max_results: int = -1
    """The maximum number of top-scored detection results to return."""

    num_threads: int = 1
    """The number of CPU threads to be used."""

    score_threshold: float = 0.0
    """The score threshold of detection results to return."""


class Rect(NamedTuple):
    """A rectangle in 2D space."""
    left: float
    top: float
    right: float
    bottom: float


class Category(NamedTuple):
    """A result of a classification task."""
    label: str
    score: float
    index: int


class Detection(NamedTuple):
    """A detected object as the result of an ObjectDetector."""
    bounding_box: Rect
    categories: List[Category]


class ObjectDetector:
    """A wrapper class for a TFLite object detection model."""

    LOC_NAME_OP = 'location'
    CAT_NAME_OP = 'category'
    SCORE_NAME_OP = 'score'
    NUM_NAME_OP = 'number of detections'

    def __init__(
            self,
            model_path: str,
            options: ObjectDetectorOptions = ObjectDetectorOptions()
    ) -> None:
        """Initialize a TFLite object detection model.
        Args:
                model_path: Path to the TFLite model.
                options: The config to initialize an object detector. (Optional)
        Raises:
                ValueError: If the TFLite model is invalid.
                OSError: If the current OS isn't supported by EdgeTPU.
        """

        # Load metadata from model.
        displayer = metadata.MetadataDisplayer.with_model_file(model_path)

        # Save model metadata for preprocessing later.
        model_metadata = json.loads(displayer.get_metadata_json())
        process_units = model_metadata['subgraph_metadata'][0]['input_tensor_metadata'][0]['process_units']
        mean = 0.0
        std = 1.0
        for option in process_units:
            if option['options_type'] == 'NormalizationOptions':
                mean = option['options']['mean'][0]
                std = option['options']['std'][0]
        self._mean = mean
        self._std = std

        # Load label list from metadata.
        file_name = displayer.get_packed_associated_file_list()[0]
        label_map_file = displayer.get_associated_file_buffer(
            file_name).decode()
        label_list = list(filter(lambda x: len(x) > 0,
                                 label_map_file.splitlines()))
        self._label_list = label_list

        interpreter = Interpreter(
            model_path=model_path, num_threads=options.num_threads)

        interpreter.allocate_tensors()
        input_detail = interpreter.get_input_details()[0]

        # From TensorFlow 2.6, the order of the outputs become undefined.
        # Therefore we need to sort the tensor indices of TFLite outputs and to know
        # exactly the meaning of each output tensor. For example, if
        # output indices are [601, 599, 598, 600], tensor names and indices aligned
        # are:
        #     - location: 598
        #     - category: 599
        #     - score: 600
        #     - detection_count: 601
        # because of the op's ports of TFLITE_DETECTION_POST_PROCESS
        # (https://github.com/tensorflow/tensorflow/blob/a4fe268ea084e7d323133ed7b986e0ae259a2bc7/tensorflow/lite/kernels/detection_postprocess.cc#L47-L50).
        sorted_output_indices = sorted(
            [output['index'] for output in interpreter.get_output_details()])
        self._output_indices = {
            self.LOC_NAME_OP: sorted_output_indices[0],
            self.CAT_NAME_OP: sorted_output_indices[1],
            self.SCORE_NAME_OP: sorted_output_indices[2],
            self.NUM_NAME_OP: sorted_output_indices[3],
        }

        self._input_size = input_detail['shape'][2], input_detail['shape'][1]
        self._is_quantized_input = input_detail['dtype'] == np.uint8
        self._interpreter = interpreter
        self._options = options

    def detect(self, input_image: np.ndarray) -> List[Detection]:
        """Run detection on an input image.
        Args:
                input_image: A [height, width, 3] RGB image. Note that height and width
                    can be anything since the image will be immediately resized according
                    to the needs of the model within this function.
        Returns:
                A Person instance.
        """
        image_height, image_width, _ = input_image.shape

        input_tensor = self._preprocess(input_image)

        self._set_input_tensor(input_tensor)
        self._interpreter.invoke()

        # Get all output details
        boxes = self._get_output_tensor(self.LOC_NAME_OP)
        classes = self._get_output_tensor(self.CAT_NAME_OP)
        scores = self._get_output_tensor(self.SCORE_NAME_OP)
        count = int(self._get_output_tensor(self.NUM_NAME_OP))

        return self._postprocess(boxes, classes, scores, count, image_width,
                                 image_height)

    def _preprocess(self, input_image: np.ndarray) -> np.ndarray:
        """Preprocess the input image as required by the TFLite model."""

        # Resize the input
        input_tensor = cv2.resize(input_image, self._input_size)

        # Normalize the input if it's a float model (aka. not quantized)
        if not self._is_quantized_input:
            input_tensor = (np.float32(input_tensor) - self._mean) / self._std

        # Add batch dimension
        input_tensor = np.expand_dims(input_tensor, axis=0)

        return input_tensor

    def _set_input_tensor(self, image):
        """Sets the input tensor."""
        tensor_index = self._interpreter.get_input_details()[0]['index']
        input_tensor = self._interpreter.tensor(tensor_index)()[0]
        input_tensor[:, :] = image

    def _get_output_tensor(self, name):
        """Returns the output tensor at the given index."""
        output_index = self._output_indices[name]
        tensor = np.squeeze(self._interpreter.get_tensor(output_index))
        return tensor

    def _postprocess(self, boxes: np.ndarray, classes: np.ndarray,
                     scores: np.ndarray, count: int, image_width: int,
                     image_height: int) -> List[Detection]:
        """Post-process the output of TFLite model into a list of Detection objects.
        Args:
                boxes: Bounding boxes of detected objects from the TFLite model.
                classes: Class index of the detected objects from the TFLite model.
                scores: Confidence scores of the detected objects from the TFLite model.
                count: Number of detected objects from the TFLite model.
                image_width: Width of the input image.
                image_height: Height of the input image.
        Returns:
                A list of Detection objects detected by the TFLite model.
        """
        results = []

        # Parse the model output into a list of Detection entities.
        for i in range(count):
            if scores[i] >= self._options.score_threshold:
                y_min, x_min, y_max, x_max = boxes[i]
                bounding_box = Rect(
                    top=int(y_min * image_height),
                    left=int(x_min * image_width),
                    bottom=int(y_max * image_height),
                    right=int(x_max * image_width))
                class_id = int(classes[i])
                category = Category(
                    score=scores[i],
                    # 0 is reserved for background
                    label=self._label_list[class_id],
                    index=class_id)
                result = Detection(bounding_box=bounding_box,
                                   categories=[category])
                results.append(result)

        # Sort detection results by score ascending
        sorted_results = sorted(
            results,
            key=lambda detection: detection.categories[0].score,
            reverse=True)

        # Filter out detections in deny list
        filtered_results = sorted_results
        if self._options.label_deny_list is not None:
            filtered_results = list(
                filter(
                    lambda detection: detection.categories[0].label not in self.
                    _options.label_deny_list, filtered_results))

        # Keep only detections in allow list
        if self._options.label_allow_list is not None:
            filtered_results = list(
                filter(
                    lambda detection: detection.categories[0].label in self._options.
                    label_allow_list, filtered_results))

        # Only return maximum of max_results detection.
        if self._options.max_results > 0:
            result_count = min(len(filtered_results),
                               self._options.max_results)
            filtered_results = filtered_results[:result_count]

        return filtered_results


_MARGIN = 20   # pixels
_ROW_SIZE = 22    # pixels
_FONT_SIZE = 3
_FONT_THICKNESS = 2
_TEXT_COLOR = (0, 255, 0)    # BGR


def visualize(image: np.ndarray, detections: List[Detection]) -> np.ndarray:

    for detection in detections:
        # Draw bounding_box
        start_point = detection.bounding_box.left, detection.bounding_box.top
        end_point = detection.bounding_box.right, detection.bounding_box.bottom
        cv2.rectangle(image, start_point, end_point, _TEXT_COLOR, 3)

        # Draw label and score
        category = detection.categories[0]
        class_name = category.label
        probability = round(category.score, 2)
        result_text = class_name + ' (' + str(probability) + ')'
        text_location = (_MARGIN + detection.bounding_box.left,
                         _MARGIN + _ROW_SIZE + detection.bounding_box.top)
        cv2.putText(image, result_text, text_location, cv2.FONT_HERSHEY_PLAIN,
                    _FONT_SIZE, _TEXT_COLOR, _FONT_THICKNESS)

    return image

def bat_detection(TFLITE_MODEL_PATH, IMG):
    DETECTION_THRESHOLD = 0.5
    image_np = IMG
    # Load the TFLite model
    options = ObjectDetectorOptions(
        num_threads=4,
        score_threshold=DETECTION_THRESHOLD,
    )
    detector = ObjectDetector(model_path=TFLITE_MODEL_PATH, options=options)

    # Run object detection estimation using the model.
    detections = detector.detect(image_np)
    # Draw keypoints and edges on input image
    image_np = visualize(image_np, detections)
    # plt.figure(figsize=(5, 5))
    # plt.grid(False)
    # plt.axis(False)
    # plt.imshow(image_np)
    return detections, image_np

def findCircle(img):
    circles = cv2.HoughCircles(img, 
                               cv2.HOUGH_GRADIENT,
                               1.2, 50,
                               param1=100,
                               param2=30,
                               minRadius=1,
                               maxRadius=20
                              )

    if circles is not None:
        # convert the (x, y) coordinates and radius of the circles to integers
        circlesRound = np.round(circles[0, :]).astype("int")
        # loop over the (x, y) coordinates and radius of the circles
        for (x, y, r) in circlesRound:
            cv2.circle(img, (x, y), r, (0, 255, 0), 4)
        return circlesRound

# Function for array in a range with respect to variable (ex: h-1, h, h+1 when thres=1)
def random_trial(var, thres):
    list_val = []
    for i in np.arange(-thres, thres, 0.1):
        list_val.append(var+i)
    return np.array(list_val)

def shots_efficiency(det, img_path, img_shape):
    x_axis = []
    y_axis = []

    # ROI co-ordinates of bat
    p1 = (det[0][0][0], det[0][0][1])
    p2 = (det[0][0][2], det[0][0][3])

    def resize_times(img, times):
        img = cv2.resize(img, (img.shape[1]*times, img.shape[0]*times))
        return img

    def nothing(x):
        # Do nothing when no changes
        pass

    # cv2.namedWindow("Adjusting value", cv2.WINDOW_NORMAL)
    # cv2.resizeWindow("Adjusting value", (400, 400))
    # cv2.createTrackbar("Threshold1", "Adjusting value", 0, 255, nothing)
    # cv2.createTrackbar("Threshold2", "Adjusting value", 0, 255, nothing)

    # while True:

    img = cv2.imread(img_path)
    img = cv2.resize(img, (img_shape[1], img_shape[0]))

    img_copy = img.copy()
    roi = img_copy[p1[1]:p2[1], p1[0]:p2[0]]
    roi_out = img_copy[p1[1]:p2[1], p1[0]:p2[0]]

    # th1 = cv2.getTrackbarPos("Threshold1", "Adjusting value")
    # th2 = cv2.getTrackbarPos("Threshold2", "Adjusting value")

    blurred = cv2.GaussianBlur(roi, (5, 5), 0)
    #     edges = cv2.Canny(blurred, th1, th2)
    edges = cv2.Canny(blurred, 50, 156)
    #     edges = cv2.Canny(blurred, 50, 200)

    kernel = np.ones((5, 5), np.uint8)
    edges_dilate = cv2.dilate(edges, kernel, iterations=1)

    contours_img = roi.copy()
    cnts = cv2.findContours(
        edges_dilate, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cnts = imutils.grab_contours(cnts)
    areas = [cv2.contourArea(c) for c in cnts]
    max_index = np.argmax(areas)
    cnt = cnts[max_index]
    cv2.drawContours(contours_img, [cnt], -1, (240, 0, 159), 1)

    # For Ball
    for i in cnt:
        x_axis.append(i[0][0])
        y_axis.append(i[0][1])
    midpoint = (((min(x_axis)+max(x_axis))//2), ((min(y_axis)+max(y_axis))//2))
    cv2.circle(contours_img, midpoint, 10, (0, 0, 255), 1)
    circlesRound = findCircle(edges)  # Center = (x, y), radius = r

    try:
        h = int(np.median(circlesRound[:, 0]))
        k = int(np.median(circlesRound[:, 1]))
        r = int(np.median(circlesRound[:, 2]))
        cv2.circle(contours_img, (h, k), r, (0, 255, 0), 1)

        dist = np.sqrt((h-midpoint[0])**2+(k-midpoint[1])**2)
        if h>min(x_axis) and h<max(x_axis) and k>min(y_axis) and k<max(y_axis):
    
            if dist < 50:
                output = "Perfect"
            else:
                output = "Edged"
    except Exception as e:
        output = "Missed"

    rs_by = 2
    edges_dilate = resize_times(edges_dilate, rs_by)

    return edges_dilate, output


# img_path = 'images/bishwa.png'
img_path = str(sys.argv[1])
model_path = 'model/bat_100.tflite'

image_np = cv2.imread(img_path)
image_np = cv2.cvtColor(image_np, cv2.COLOR_BGR2RGB)
try:
    text_shot, imgRGB = predict_shot(image_np)
except:
    text_shot = "Unknown"

# Bat
image = Image.open(img_path).convert('RGB')
image.thumbnail((512, 512), Image.ANTIALIAS)
image_bat = np.asarray(image)
plt.imshow(image_bat)
try:
    det, img = bat_detection(model_path, image_bat)
    img_shape = img.shape
    img_edge, text_eff = shots_efficiency(det, img_path, img_shape)
except:
    text_eff = "Unknown"

# while True:
#     cv2.imshow("Image", img)
#     k = cv2.waitKey(1) & 0xff
#     if k == 27:  # If backspace break
#         break
# cv2.destroyAllWindows()

text = f"Shot:{text_shot}, Efficiency: {text_eff}"
print(text)
plt.figure(figsize=(10, 10))
plt.text(0, -5, text, size='xx-large', weight=500)
plt.imshow(img)
plt.grid(False)
plt.axis(False)
plt.show()
