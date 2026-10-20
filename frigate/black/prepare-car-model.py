"""Prepare the Open Model Zoo RGB vehicle-reid-0001 ONNX for CUDA-only inference.

Run on a server in the existing Frigate runtime, without network or added packages.
The original graph and weights are preserved; only the documented input is fixed.
"""
import argparse
from pathlib import Path

MODEL_URL = ("https://drive.usercontent.google.com/download?"
             "id=1MEtaIr_9mWuntGD9edydFl_T5waloNRm&export=download&confirm=t")
MODEL_SOURCE = "https://github.com/openvinotoolkit/open_model_zoo/tree/master/models/public/vehicle-reid-0001"
SOURCE_BYTES = 8_836_743


def prepare(source, target):
    import onnx

    source, target = Path(source), Path(target)
    if source.stat().st_size != SOURCE_BYTES:
        raise ValueError("Original model size differs from the official manifest")
    model = onnx.load(str(source))
    if len(model.graph.input) != 1 or model.graph.input[0].name != "input":
        raise ValueError("Unexpected vehicle model input")
    dimensions = model.graph.input[0].type.tensor_type.shape.dim
    if len(dimensions) != 4:
        raise ValueError("Unexpected vehicle model dimensions")
    for dimension, value in zip(dimensions, (1, 3, 208, 208)):
        dimension.ClearField("dim_param")
        dimension.dim_value = value
    model = onnx.shape_inference.infer_shapes(model)
    onnx.checker.check_model(model)
    onnx.save(model, str(target))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("target")
    args = parser.parse_args()
    prepare(args.source, args.target)
