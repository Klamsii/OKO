"""Automatically focus the visible desk worker's hands in original frames."""
from pathlib import Path


def wrist_region(boxes, wrists, confidences, image_size):
    """Return a pixel crop for the best two-wrist pose, or None if unreliable."""
    width, height = image_size
    candidates = []
    for box, pair, scores in zip(boxes, wrists, confidences):
        if len(pair) != 2 or len(scores) != 2 or min(scores) < 0.35:
            continue
        x1, y1, x2, y2 = (float(v) for v in box)
        if not 0 <= x1 < x2 <= width or not 0 <= y1 < y2 <= height:
            continue
        xs = [float(point[0]) for point in pair]
        ys = [float(point[1]) for point in pair]
        if not all(0 <= x < width for x in xs) or not all(0 <= y < height for y in ys):
            continue
        margin_x = max(55, 0.65 * (x2 - x1))
        margin_y = max(55, 0.6 * (y2 - y1))
        crop = (round(max(0, min(xs) - margin_x)),
                round(max(0, min(ys) - margin_y)),
                round(min(width, max(xs) + margin_x)),
                round(min(height, max(ys) + margin_y)))
        if crop[2] - crop[0] < 24 or crop[3] - crop[1] < 24:
            continue
        # The clearer pair wins over a faint reflection, independent of position.
        candidates.append((min(scores), crop))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


class PoseHandCropper:
    def __init__(self, weights):
        import hashlib
        from ultralytics import YOLO
        weights = Path(weights)
        if not weights.is_file():
            raise FileNotFoundError(weights)
        with weights.open('rb') as stream:
            self.signature = hashlib.file_digest(stream, 'sha256').hexdigest()[:16]
        self.model = YOLO(str(weights))

    def crop(self, source, destination):
        from PIL import Image
        source, destination = Path(source), Path(destination)
        with Image.open(source) as original:
            frame = original.convert('RGB')
        result = self.model.predict(str(source), device='cpu', imgsz=960,
                                    conf=0.2, verbose=False)[0]
        if result.keypoints is None or len(result.keypoints.xy) == 0:
            return None
        boxes = result.boxes.xyxy.cpu().numpy().tolist()
        wrists = result.keypoints.xy[:, 9:11].cpu().numpy().tolist()
        scores = result.keypoints.conf[:, 9:11].cpu().numpy().tolist()
        bounds = wrist_region(boxes, wrists, scores, frame.size)
        if bounds is None:
            return None
        cropped = frame.crop(bounds)
        scale = min(4, 1008 / max(cropped.size))
        cropped = cropped.resize((round(cropped.width * scale),
                                  round(cropped.height * scale)), Image.Resampling.LANCZOS)
        destination.parent.mkdir(parents=True, exist_ok=True)
        cropped.save(destination)
        return dict(path=str(destination), bounds_pixels=bounds,
                    source_size=frame.size)
