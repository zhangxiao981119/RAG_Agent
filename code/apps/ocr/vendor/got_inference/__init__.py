"""GOT-OCR2.0 官方推理代码（vendored）。

来源：HF 仓 stepfun-ai/GOT-OCR2_0 的 modeling_GOT.py 与 got_vision_b.py，
按原文件整体复制，未做任何改写。

固化原因：官方推理入口 GOTQwenForCausalLM.chat 的 render=True 分支引用
verovio（仅乐谱/公式渲染用，本项目用不到）。若走 transformers 的
trust_remote_code 动态加载，check_imports 会因缺少 verovio 直接拒绝加载
整个模块；复制进本包后按普通模块本地 import，check_imports 不参与，
verovio 无需安装。

★ 本目录文件 MUST NOT 手工修改：升级时从上游整体覆盖。
  CPU 适配（去 CUDA / 去 autocast / fp32）放在 apps/ocr/main.py 的 _chat_cpu，
  不落在 vendored 文件里。
"""
from .modeling_GOT import (  # noqa: F401
    DEFAULT_IMAGE_PATCH_TOKEN,
    DEFAULT_IMAGE_TOKEN,
    DEFAULT_IM_END_TOKEN,
    DEFAULT_IM_START_TOKEN,
    Conversation,
    GOTConfig,
    GOTImageEvalProcessor,
    GOTQwenForCausalLM,
    GOTQwenModel,
    KeywordsStoppingCriteria,
    SeparatorStyle,
)

__all__ = [
    "DEFAULT_IMAGE_PATCH_TOKEN",
    "DEFAULT_IMAGE_TOKEN",
    "DEFAULT_IM_END_TOKEN",
    "DEFAULT_IM_START_TOKEN",
    "Conversation",
    "GOTConfig",
    "GOTImageEvalProcessor",
    "GOTQwenForCausalLM",
    "GOTQwenModel",
    "KeywordsStoppingCriteria",
    "SeparatorStyle",
]
