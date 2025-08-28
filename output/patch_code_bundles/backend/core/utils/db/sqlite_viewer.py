import subprocess
import sqlite3
import os
import sys
import runpy
import tkinter as tk
from tkinter import messagebox

class SQLiteWebWrapperGUI:
    def __init__(self):
        # ✅ Hardcode your SQLite DB path here:
        self.db_path = "C:\\Users\\cg371\\PycharmProjects\\ChatGPT Bot\\databases\\bot_dev.db"
        self.tables = []
        sys.argv = ["sqlite_web", self.db_path]

        if not os.path.exists(self.db_path):
            messagebox.showerror("Error", f"Database file not found:\n{self.db_path}")
            sys.exit(1)

    def list_tables(self):
        """Retrieve all table names in the selected DB."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
        )
        self.tables = [row[0] for row in cursor.fetchall()]
        conn.close()
        if not self.tables:
            messagebox.showinfo("Info", "No tables found in the database.")
            sys.exit(1)

    def prompt_table_selection(self):
        """Show a listbox for selecting which tables to view."""
        selected = []

        def on_submit():
            selected_indices = listbox.curselection()
            for i in selected_indices:
                selected.append(self.tables[i])
            window.destroy()

        window = tk.Tk()
        window.title("Select Tables")
        window.geometry("300x400")

        label = tk.Label(window, text="Select tables to view in sqlite-web:")
        label.pack(pady=10)

        listbox = tk.Listbox(window, selectmode=tk.MULTIPLE, exportselection=False)
        for t in self.tables:
            listbox.insert(tk.END, t)
        listbox.pack(expand=True, fill=tk.BOTH, padx=10, pady=10)

        btn = tk.Button(window, text="Launch sqlite-web", command=on_submit)
        btn.pack(pady=10)

        window.mainloop()
        return selected

    def launch_sqlite_web(self, selected_tables):
        """Launch sqlite-web on the DB."""
        print(f"Launching sqlite-web on: {self.db_path}")
        print(f"Selected tables: {selected_tables}")
        try:
            runpy.run_module("sqlite_web", run_name="__main__", alter_sys=True)
            #subprocess.run(["sqlite_web", self.db_path])
        except FileNotFoundError:
            messagebox.showerror("Error", "sqlite-web is not installed.\nRun `pip install sqlite-web`.")
        except KeyboardInterrupt:
            print("Closed by user.")

    def run(self):
        """Main flow."""
        self.list_tables()
        selected_tables = self.prompt_table_selection()
        if not selected_tables:
            messagebox.showinfo("Cancelled", "No tables selected.")
            sys.exit(0)
        self.launch_sqlite_web(selected_tables)


if __name__ == "__main__":
    viewer = SQLiteWebWrapperGUI()
    viewer.run()



