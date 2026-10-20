"""Extract the CLS image features of the pinned DINOv2-S model.

Run offline on the server in the existing Frigate image. The input is the FP32
ONNX graph, not executable model code. No package installation needed.
"""

import argparse
from pathlib import Path


MODEL_COMMIT = "8b1f705a3a7f6f062f6bdd21986c1583d3ef105d"
MODEL_URL = ("https://huggingface.co/onnx-community/dinov2-small/resolve/" + MODEL_COMMIT
             + "/onnx/model.onnx")


def prepare(source, target):
    import onnx
    from onnx import TensorProto, helper

    model = onnx.load(source, load_external_data=False)
    if (len(model.graph.input) != 1 or len(model.graph.output) != 1
            or model.graph.input[0].name != "pixel_values"
            or model.graph.output[0].name != "last_hidden_state"
            or model.graph.output[0].type.tensor_type.shape.dim[-1].dim_value != 384
            or any(x.data_location == TensorProto.EXTERNAL for x in model.graph.initializer)):
        raise ValueError("Unexpected model graph; refusing an unverified model")
    for dim, value in zip(model.graph.input[0].type.tensor_type.shape.dim, [1, 3, 224, 224]):
        dim.ClearField("dim_param")
        dim.dim_value = value
    model.graph.initializer.append(helper.make_tensor("cls_index", TensorProto.INT64, [], [0]))
    model.graph.node.append(helper.make_node("Gather", ["last_hidden_state", "cls_index"],
                                             ["cls_features"], axis=1))
    del model.graph.output[:]
    model.graph.output.append(helper.make_tensor_value_info("cls_features", TensorProto.FLOAT, [1, 384]))
    model.graph.name = "dinov2-small-cls-v1"
    onnx.checker.check_model(model)
    onnx.save(model, target)
    print(f"Prepared {Path(target).name}: {Path(target).stat().st_size} bytes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("target")
    args = parser.parse_args()
    prepare(args.source, args.target)
