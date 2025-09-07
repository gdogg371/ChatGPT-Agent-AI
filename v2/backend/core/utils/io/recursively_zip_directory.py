#!/usr/bin/env python3
import os
import zipfile

# ==== CONFIG ====
SOURCE_FOLDER = r"C:\Users\cg371\Code Repos\Lineage"     # Folder to zip
OUTPUT_ZIP = r"C:\Users\cg371\Code Repos\Lineage.zip"    # Output archive path
# ================

def zip_directory(folder_path, output_filename):
    folder_path = os.path.abspath(folder_path)
    top_dir_name = os.path.basename(folder_path.rstrip(os.sep))

    with zipfile.ZipFile(output_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(folder_path):
            for file in files:
                abs_file_path = os.path.join(root, file)
                # Ensure top_dir_name is the root folder in the archive
                rel_path_in_zip = os.path.join(top_dir_name, os.path.relpath(abs_file_path, folder_path))
                zipf.write(abs_file_path, rel_path_in_zip)

    print(f"Created archive: {output_filename}")

if __name__ == "__main__":
    if not os.path.isdir(SOURCE_FOLDER):
        raise ValueError(f"Source folder does not exist: {SOURCE_FOLDER}")

    zip_directory(SOURCE_FOLDER, OUTPUT_ZIP)
