import os

# 🔧 Set your target directory here
TARGET_DIR = "/v1/backend\\core\\"
#TARGET_DIR = "G:\\My Drive\\ComfyUI\\packages\\comfyui-packages\\"

def crawl_directory(path):
    structure = {}
    for root, dirs, files in os.walk(path):
        #print(dirs, files)
        # Build nested dictionary keys
        rel_path = os.path.relpath(root, path)
        parts = rel_path.split(os.sep) if rel_path != '.' else []
        current = structure
        if 'templates' not in parts:
            for part in parts:
                current = current.setdefault(part, {})
        current["__files__"] = files
    return structure

def print_yaml_like(structure, indent=0):
    for key, value in structure.items():
        if key == "__files__":
            for f in value:
                print("  " * indent + f"- {f}")
                #pass
        else:
            print("  " * indent + f"{key}/")
            print_yaml_like(value, indent + 1)
            #pass

if __name__ == "__main__":
    print(f"\n📁 Directory structure under: {os.path.abspath(TARGET_DIR)}\n")
    result = crawl_directory(TARGET_DIR)
    print_yaml_like(result)
