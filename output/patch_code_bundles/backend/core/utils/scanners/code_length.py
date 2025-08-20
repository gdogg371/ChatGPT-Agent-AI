import os
from collections import defaultdict

# Define your root directory
ROOT_DIR = r"/"

# Define script-like file extensions
SCRIPT_EXTENSIONS = {'.py', '.sh', '.bat', '.ps1', '.js', '.ts', '.rb', '.pl', '.lua', '.php', '.r', '.java', '.cpp', '.c', '.cs'}

# Initialize counters
overall_incl_blank = 0
overall_excl_blank = 0
by_ext = defaultdict(lambda: {'incl': 0, 'excl': 0})

def is_script_file(filename):
    return os.path.splitext(filename)[1].lower() in SCRIPT_EXTENSIONS

# Walk through the directory
for root, _, files in os.walk(ROOT_DIR):
    for file in files:
        if is_script_file(file):
            ext = os.path.splitext(file)[1].lower()
            file_path = os.path.join(root, file)
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()
                    incl = len(lines)
                    excl = sum(1 for line in lines if line.strip())
                    overall_incl_blank += incl
                    overall_excl_blank += excl
                    by_ext[ext]['incl'] += incl
                    by_ext[ext]['excl'] += excl
            except Exception as e:
                print(f"⚠️ Error reading {file_path}: {e}")

# Print totals
print(f"\n📊 OVERALL TOTALS")
print(f"Total lines (including whitespace): {overall_incl_blank}")
print(f"Total lines (excluding whitespace): {overall_excl_blank}")

print(f"\n📊 BREAKDOWN BY FILE EXTENSION")
for ext, counts in sorted(by_ext.items()):
    print(f"{ext}: {counts['incl']} lines (incl), {counts['excl']} lines (excl)")
