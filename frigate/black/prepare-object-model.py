"""Extract the spatially pooled features of the pinned MobileNetV2 model.

Run offline on the server in the existing Frigate image. The input is the FP32
ONNX Model Zoo file, not executable model code. No package installation needed.
"""

import argparse
from pathlib import Path


MODEL_COMMIT = "c5cae0f1942c8d7727372c7b1f6b485aa39e4421"
MODEL_URL = ("https://media.githubusercontent.com/media/onnx/models/" + MODEL_COMMIT
             + "/validated/vision/classification/mobilenet/model/mobilenetv2-12.onnx")


def prepare(source, target):
    import onnx
    from onnx import TensorProto, helper

    model = onnx.load(source)
    pool = next(x for x in model.graph.node if x.op_type == "GlobalAveragePool")
    if pool.output != ["464"] or model.graph.input[0].name != "input":
        raise ValueError("Unexpected model graph; refusing an unverified model")
    nodes = list(model.graph.node)
    nodes = nodes[:nodes.index(pool) + 1]
    weights = {name for node in nodes for name in node.input}
    graph = helper.make_graph(nodes, "mobilenetv2-12-gap-v1",
        [helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, 224, 224])],
        [helper.make_tensor_value_info("464", TensorProto.FLOAT, [1, 1280, 1, 1])],
        [x for x in model.graph.initializer if x.name in weights])
    result = helper.make_model(graph, opset_imports=list(model.opset_import))
    result.ir_version = model.ir_version
    onnx.checker.check_model(result)
    onnx.save(result, target)
    print(f"Prepared {Path(target).name}: {Path(target).stat().st_size} bytes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("target")
    args = parser.parse_args()
    prepare(args.source, args.target)
