import os
import requests
from pathlib import Path
from tqdm import tqdm
from secret_management.secrets_loader import get_secret

# === CONFIGURATION ===

ROOT = Path(r"/software")

HF_TOKEN = get_secret("hugging_face", default="")  # looks in secret_management/, then ~/.config/packager/  # <<< INSERT YOUR HF TOKEN HERE
HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"}

DIR_STRUCTURE = {
    "ai_models/mistral": {
        "urls": [
            (
                "https://huggingface.co/TheBloke/Mistral-7B-Instruct-GGUF/resolve/main/mistral-7b-instruct-v0.1.Q4_K_M.gguf",
                "mistral-7b-instruct-v0.1.Q4_K_M.gguf"
            ),
            (
                "https://huggingface.co/mistralai/Mistral-7B-Instruct-v0.1/resolve/main/tokenizer.model",
                "tokenizer.model"
            )
        ]
    },
    "ai_models/phi": {
        "urls": [
            (
                "https://huggingface.co/TheBloke/phi-2-GGUF/resolve/main/phi-2.Q4_K_M.gguf",
                "phi-2.Q4_K_M.gguf"
            )
        ]
    },
    "ai_models/codellama": {
        "urls": [
            (
                "https://huggingface.co/TheBloke/CodeLlama-7B-Instruct-GGUF/resolve/main/codellama-7b-instruct.Q4_K_M.gguf",
                "codellama-7b-instruct.Q4_K_M.gguf"
            )
        ]
    },
    "nlp_tools/fasttext": {
        "urls": [
            (
                "https://dl.fbaipublicfiles.com/fasttext/vectors-crawl/cc.en.300.bin.gz",
                "cc.en.300.bin.gz"
            )
        ]
    },
    "nlp_tools/sentencepiece": {
        "urls": [
            (
                "https://huggingface.co/google/sentencepiece/resolve/main/spm.model",
                "spm.model"
            )
        ]
    },
    "vision_models/yolov8": {
        "urls": [
            (
                "https://github.com/ultralytics/assets/releases/download/v0.0.0/yolov8n.pt",
                "yolov8n.pt"
            )
        ]
    },
    "vision_models/tesseract": {
        "urls": [
            (
                "https://github.com/tesseract-ocr/tessdata_best/raw/main/eng.traineddata",
                "eng.traineddata"
            )
        ]
    },
    "grammars": {
        "urls": [
            (
                "https://raw.githubusercontent.com/lark-parser/lark/master/lark/grammars/python.lark",
                "python.lark"
            ),
            (
                "https://raw.githubusercontent.com/trufflesecurity/trufflehog/main/config/regex_patterns.json",
                "standard_patterns.json"
            ),
            (
                "https://raw.githubusercontent.com/your-org/your-repo/main/grammars/cli_commands.tx",
                "cli_commands.tx"
            ),
            (
                "https://raw.githubusercontent.com/your-org/your-repo/main/grammars/goal.lark",
                "goal.lark"
            )
        ]
    },
    "cli_binaries": {
        "urls": [
            (
                "https://github.com/PyCQA/bandit/releases/latest/download/bandit",
                "bandit"
            ),
            (
                "https://github.com/returntocorp/semgrep/releases/latest/download/semgrep",
                "semgrep"
            ),
            (
                "https://github.com/tree-sitter/tree-sitter/releases/latest/download/tree-sitter",
                "tree-sitter"
            ),
            (
                "https://raw.githubusercontent.com/PyCQA/bandit/main/bandit/config.yaml",
                "bandit_rules.yaml"
            )
        ]
    },
    "embeddings": {
        "urls": [
            (
                "https://huggingface.co/datasets/your-org/code-embeddings/resolve/main/code_embeddings.faiss",
                "code_embeddings.faiss"
            ),
            (
                "https://huggingface.co/datasets/your-org/goal-vectors/resolve/main/goal_vectors.jsonl",
                "goal_vectors.jsonl"
            )
        ]
    },
    "weights": {
        "urls": [
            (
                "https://huggingface.co/datasets/your-org/trust-scores/resolve/main/trust_score_weights.json",
                "trust_score_weights.json"
            ),
            (
                "https://huggingface.co/datasets/your-org/patch-risk/resolve/main/risk_weights.csv",
                "risk_weights.csv"
            ),
            (
                "https://huggingface.co/datasets/your-org/scheduler-state/resolve/main/scheduler_matrix.npy",
                "scheduler_matrix.npy"
            )
        ]
    }
}


GITIGNORE_CONTENT = "*\n!.gitignore\n"

# === UTILITIES ===

def ensure_dir_with_gitignore(path):
    if not os.path.exists(path):
        os.makedirs(path)
        print(f"📁 Created: {path}")
    else:
        print(f"📦 Exists:  {path}")
    gitignore_path = os.path.join(path, ".gitignore")
    if not os.path.exists(gitignore_path):
        with open(gitignore_path, "w") as f:
            f.write(GITIGNORE_CONTENT)
        print(f"🛡️  .gitignore added to: {path}")

def download_file(url, dest_path, headers=None):
    try:
        with requests.get(url, stream=True, headers=headers) as r:
            r.raise_for_status()
            total = int(r.headers.get('content-length', 0))
            with open(dest_path, 'wb') as f, tqdm(
                total=total,
                unit='B',
                unit_scale=True,
                desc=dest_path.name,
                ncols=80
            ) as pbar:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
                    pbar.update(len(chunk))
        print(f"✅ Download complete: {dest_path}")
    except Exception as e:
        print(f"❌ Download failed for {url} → {e}")

# === MAIN ===

def main():
    print(f"\n🚀 Bootstrapping: {ROOT}\n")

    for rel_path, meta in DIR_STRUCTURE.items():
        full_path = ROOT / rel_path
        ensure_dir_with_gitignore(full_path)

        for url, filename in meta.get("urls", []):
            file_path = full_path / filename
            if file_path.exists():
                print(f"⏩ Skipping existing file: {file_path}")
            else:
                print(f"⬇️  Downloading {filename} to {file_path.parent}")
                download_file(url, file_path, headers=HEADERS)

    print("\n✅ All setup complete.")

if __name__ == "__main__":
    main()
