"""Fixed HUST source-method audit inputs, separate from runtime qualification.

Files are downloaded explicitly into artifacts; audit imports never fetch or
execute engine modules. SHA checks precede extraction of complete method bodies.
"""

CED_PROFILE = "hust-cedb477-native-v1"
CED_COMMIT = "ced6857afa0ea7b2e3f0846a62e1394e90f15607"
CED_TERMINAL_HASHES = {
    "scheduler.py": "c1db45f3bbad3a875dd8638331b8870c4380ac1ab4734265ba5c863833a82884",
    "core.py": "268bf40534a853812867ae8e80040f190983cb364b41182b55969aace20c93f5",
    "output_processor.py": (
        "e47b86cd69ce1c1e3f53dac655eaa75c32a4057d10f4a1db16bb83a397f2a329"
    ),
    "core_client.py": (
        "fd6ca0cb7499b3086eef2fc57674b7ace56ef1c648f09e92e144b255b79018f8"
    ),
}
CED_CAPTURE_HASHES = CED_TERMINAL_HASHES | {
    "async_llm.py": "52cbf404f1dbc99c6bebf4a50a7eb18ff842a1f53106a45f365523192d816be1",
    "input_processor.py": (
        "c8abd8e4e14d99929d45a7ff3b2a74898269c5e2a7fa30b826623ee85e4ce813"
    ),
    "parallel_sampling.py": (
        "bf3f0a3c6640aaf706d1b6f7ec3a8e09a970f3dd9e7e96fa6a901779ea84f61d"
    ),
}
CED_ENTRY_HASHES = {
    # BaseServing moved to entrypoints/serve/engine/serving.py.
    "engine_serving.py": (
        "70143311acc2230b925e7f3603b8b9314f029ff5e98d5af990b3bdca691331f1"
    ),
    "completion_serving.py": (
        "75d05ddddf303734a078a3e42fcb173fef58ea56da09bc0b5c7525cb9bbbf527"
    ),
    "chat_serving.py": (
        "ea1f76074a9587c8054f54d30a6ba748b6a5fc4d90dd82c801504505c1da1f92"
    ),
}
CED_SOURCE_PATHS = {
    "scheduler.py": "vllm/v1/core/sched/scheduler.py",
    "core.py": "vllm/v1/engine/core.py",
    "output_processor.py": "vllm/v1/engine/output_processor.py",
    "core_client.py": "vllm/v1/engine/core_client.py",
    "async_llm.py": "vllm/v1/engine/async_llm.py",
    "input_processor.py": "vllm/v1/engine/input_processor.py",
    "parallel_sampling.py": "vllm/v1/engine/parallel_sampling.py",
    "engine_serving.py": "vllm/entrypoints/serve/engine/serving.py",
    "completion_serving.py": "vllm/entrypoints/openai/completion/serving.py",
    "chat_serving.py": "vllm/entrypoints/openai/chat_completion/serving.py",
}


def is_ced_profile(profile):
    if profile not in ("hust-legacy-publisher-v1", CED_PROFILE):
        raise ValueError("Unknown source audit profile")
    return profile == CED_PROFILE
