"""Attach a train-only fitted G2/V6/other head to a local SigLIP 2 vision ONNX.

This prepares a graph without running inference or downloading model weights.
The resulting graph is only usable after GPU audit and independent calibration.
"""
import argparse
import json
from pathlib import Path


def prepare(vision, head, destination):
    import numpy as np
    import onnx
    from onnx import TensorProto, helper, numpy_helper
    from ocr.frigate_object_worker import TWO_WHEELER_CLASSES

    graph_model = onnx.load(str(vision))
    outputs = [x for x in graph_model.graph.output if x.name == "pooler_output"]
    if len(graph_model.graph.input) != 1 or len(outputs) != 1:
        raise ValueError("Expected a SigLIP 2 vision graph with pooler_output")
    with np.load(head, allow_pickle=False) as fitted:
        coef = np.asarray(fitted["coef"], dtype=np.float32)
        intercept = np.asarray(fitted["intercept"], dtype=np.float32)
    if (coef.shape != (3, 768) or intercept.shape != (3,)
            or not np.isfinite(coef).all() or not np.isfinite(intercept).all()):
        raise ValueError("Expected three finite classifier rows for 768 features")
    dimensions = graph_model.graph.input[0].type.tensor_type.shape.dim
    if len(dimensions) != 4:
        raise ValueError("Expected a single NCHW image tensor")
    for dimension, value in zip(dimensions, [1, 3, 224, 224]):
        dimension.ClearField("dim_param")
        dimension.dim_value = value
    initializers = {
        "kugoo_head_weights": coef.T.copy(), "kugoo_head_bias": intercept,
        "kugoo_head_epsilon": np.array(1e-9, dtype=np.float32),
    }
    graph_model.graph.initializer.extend(numpy_helper.from_array(value, name)
                                        for name, value in initializers.items())
    graph_model.graph.node.extend([
        helper.make_node("ReduceL2", ["pooler_output"], ["kugoo_head_norm"], axes=[1], keepdims=1),
        helper.make_node("Max", ["kugoo_head_norm", "kugoo_head_epsilon"], ["kugoo_head_denominator"]),
        helper.make_node("Div", ["pooler_output", "kugoo_head_denominator"], ["kugoo_head_features"]),
        helper.make_node("MatMul", ["kugoo_head_features", "kugoo_head_weights"], ["kugoo_head_product"]),
        helper.make_node("Add", ["kugoo_head_product", "kugoo_head_bias"], ["kugoo_head_logits"]),
        helper.make_node("Softmax", ["kugoo_head_logits"], ["two_wheeler_probabilities"], axis=1),
    ])
    del graph_model.graph.output[:]
    graph_model.graph.output.append(helper.make_tensor_value_info(
        "two_wheeler_probabilities", TensorProto.FLOAT, [1, 3]))
    properties = {item.key: item.value for item in graph_model.metadata_props}
    properties["two_wheeler_classes"] = json.dumps(TWO_WHEELER_CLASSES)
    properties["image_preprocessing"] = "RGB; resize 224x224 bicubic; (pixel/255-0.5)/0.5"
    helper.set_model_props(graph_model, properties)
    onnx.checker.check_model(graph_model)
    onnx.save(graph_model, str(destination))
    print(json.dumps({"output": str(destination), "bytes": Path(destination).stat().st_size,
                      "classes": TWO_WHEELER_CLASSES}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vision", type=Path)
    parser.add_argument("head", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    prepare(arguments.vision, arguments.head, arguments.destination)
