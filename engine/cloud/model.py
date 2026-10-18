"""BF16 H100 loader sharing the existing single-image inference methods."""
import hashlib
import json
import os
from pathlib import Path

from peoplewatch.detector import QwenPeopleDetector, PROMPT


class H100Detector(QwenPeopleDetector):
    def __init__(self, model_path, adapter=None):
        for key in ('HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE', 'HF_HUB_DISABLE_TELEMETRY'):
            os.environ[key] = '1'
        import torch
        import transformers
        from transformers import AutoProcessor, Qwen3VLForConditionalGeneration
        if not torch.cuda.is_available():
            raise RuntimeError('CUDA GPU is required; preflight before starting a workload')
        path = Path(model_path).resolve(strict=True)
        self.torch = torch
        torch.manual_seed(1234)
        self.model = Qwen3VLForConditionalGeneration.from_pretrained(
            str(path), local_files_only=True, dtype=torch.bfloat16,
            attn_implementation='sdpa', device_map={'': 'cuda:0'}).eval()
        adapter_manifest = None
        if adapter:
            from peft import PeftModel
            adapter = Path(adapter).resolve(strict=True)
            self.model = PeftModel.from_pretrained(self.model, str(adapter),
                local_files_only=True, is_trainable=False).eval()
            adapter_manifest = [(p.name, hashlib.sha256(p.read_bytes()).hexdigest())
                for p in sorted(adapter.iterdir()) if p.is_file()]
        self.processor = AutoProcessor.from_pretrained(str(path), local_files_only=True)
        # Same prompts and image processing as local production; no label/reference import.
        self.metadata = {'model': str(path), 'torch': torch.__version__,
            'transformers': transformers.__version__, 'quantization': 'none/BF16',
            'gpu': torch.cuda.get_device_name(), 'prompt': PROMPT,
            'adapter': str(adapter) if adapter else None, 'adapter_manifest': adapter_manifest}
        manifest = [(p.name, p.stat().st_size, p.stat().st_mtime_ns)
                    for p in sorted(path.iterdir()) if p.is_file()]
        self.signature = hashlib.sha256(json.dumps([self.metadata, manifest],
            sort_keys=True).encode()).hexdigest()
