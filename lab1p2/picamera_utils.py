import cv2

class BoundingBox:
    def __init__(self, origin_x, origin_y, width, height):
        self.origin_x = origin_x
        self.origin_y = origin_y
        self.width = width
        self.height = height

class Category:
    def __init__(self, category_name, score):
        self.category_name = category_name
        self.score = score

class Detection:
    def __init__(self, bounding_box, categories):
        self.bounding_box = bounding_box
        self.categories = categories


def create_detections(boxes, classes, scores, labels):
    detections = []
    for i in range(len(boxes)):
        if scores[i] < 0.4:  # Skip low-confidence detections
            continue
        ymin, xmin, ymax, xmax = boxes[i]


        bbox = BoundingBox(xmin, ymin, xmax - xmin, ymax - ymin)
        category = Category(labels[int(classes[i])], scores[i])
        detections.append(Detection(bbox, [category]))
    return detections


def visualize(image, detection_result):
    """Draws bounding boxes on the input image and returns it."""
    height, width, _ = image.shape

    for detection in detection_result:
        # Draw bounding_box
        bbox = detection.bounding_box

        # Convert normalized coordinates to absolute pixel values; ensure coordinates are correctly interpreted
        xmin = int(bbox.origin_x * width)
        ymin = int(bbox.origin_y * height)
        xmax = int((bbox.origin_x + bbox.width) * width)
        ymax = int((bbox.origin_y + bbox.height) * height)

        if xmin != xmax and ymin != ymax:
            cv2.rectangle(image, (xmin, ymin), (xmax, ymax), (0, 255, 0), 3)

        # Draw label and score
        category = detection.categories[0]
        category_name = category.category_name
        probability = round(category.score, 2)
        result_text = f"{category_name} ({probability})"
        text_location = (xmin, ymin - 10 if ymin > 10 else ymin + 15)
        cv2.putText(image, result_text, text_location, cv2.FONT_HERSHEY_PLAIN, 1, (0, 255, 0), 2)

    return image