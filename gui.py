#!/usr/bin/env python3
"""
Proste GUI do scrapera Confluence:
- listuje strony
- pozwala wybrac zakres / zaznaczenie
- pobiera HTML zaznaczonych stron
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from scrape_confluence import discover_pages, scrape

DEFAULT_URL = (
    "https://omega-psir.atlassian.net/wiki/spaces/OM/overview?homepageId=557186"
)


class ConfluenceGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Confluence scraper")
        self.geometry("920x680")
        self.minsize(760, 520)

        self.pages: list[tuple[str, str]] = []  # (title, url)
        self._busy = False
        self._log_queue: queue.Queue[str] = queue.Queue()

        self._build()
        self.after(200, self._drain_log)

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="URL przestrzeni / strony:").pack(anchor="w")
        url_row = ttk.Frame(top)
        url_row.pack(fill="x")
        self.url_var = tk.StringVar(value=DEFAULT_URL)
        ttk.Entry(url_row, textvariable=self.url_var).pack(
            side="left", fill="x", expand=True
        )

        opts = ttk.Frame(top)
        opts.pack(fill="x", pady=(6, 0))
        self.headed_var = tk.BooleanVar(value=False)
        self.deep_var = tk.BooleanVar(value=False)
        self.only_under_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opts, text="Pokaz przegladarke (logowanie)", variable=self.headed_var
        ).pack(side="left")
        ttk.Checkbutton(
            opts,
            text="Gleboka lista (BFS, wolniejsza)",
            variable=self.deep_var,
        ).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(
            opts, text="Tylko podgalaz", variable=self.only_under_var
        ).pack(side="left", padx=(12, 0))

        out_row = ttk.Frame(top)
        out_row.pack(fill="x", pady=(6, 0))
        ttk.Label(out_row, text="Plik HTML:").pack(side="left")
        self.output_var = tk.StringVar(
            value=str(Path.cwd() / "confluence_export.html")
        )
        ttk.Entry(out_row, textvariable=self.output_var).pack(
            side="left", fill="x", expand=True, padx=(6, 6)
        )
        ttk.Button(out_row, text="...", width=3, command=self._pick_output).pack(
            side="left"
        )

        actions = ttk.Frame(top)
        actions.pack(fill="x", pady=(8, 0))
        self.btn_list = ttk.Button(
            actions, text="1. Pobierz liste", command=self._start_list
        )
        self.btn_list.pack(side="left")
        self.btn_export = ttk.Button(
            actions,
            text="2. Pobierz zaznaczone do HTML",
            command=self._start_export,
        )
        self.btn_export.pack(side="left", padx=(8, 0))

        range_fr = ttk.LabelFrame(self, text="Zakres (numery z listy)")
        range_fr.pack(fill="x", **pad)
        rr = ttk.Frame(range_fr)
        rr.pack(fill="x", padx=8, pady=6)
        ttk.Label(rr, text="Od:").pack(side="left")
        self.from_var = tk.StringVar(value="1")
        self.to_var = tk.StringVar(value="1")
        ttk.Entry(rr, textvariable=self.from_var, width=6).pack(side="left", padx=4)
        ttk.Label(rr, text="Do:").pack(side="left")
        ttk.Entry(rr, textvariable=self.to_var, width=6).pack(side="left", padx=4)
        ttk.Button(rr, text="Zaznacz zakres", command=self._select_range).pack(
            side="left", padx=(10, 0)
        )
        ttk.Button(rr, text="Zaznacz wszystko", command=self._select_all).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(rr, text="Odznacz", command=self._clear_sel).pack(
            side="left", padx=(6, 0)
        )
        self.sel_label = ttk.Label(rr, text="Zaznaczono: 0")
        self.sel_label.pack(side="right")

        list_fr = ttk.LabelFrame(self, text="Strony")
        list_fr.pack(fill="both", expand=True, **pad)
        cols = ("nr", "title", "url")
        self.tree = ttk.Treeview(
            list_fr,
            columns=cols,
            show="headings",
            selectmode="extended",
        )
        self.tree.heading("nr", text="#")
        self.tree.heading("title", text="Tytul")
        self.tree.heading("url", text="URL")
        self.tree.column("nr", width=50, stretch=False, anchor="center")
        self.tree.column("title", width=320)
        self.tree.column("url", width=480)
        scroll_y = ttk.Scrollbar(list_fr, orient="vertical", command=self.tree.yview)
        scroll_x = ttk.Scrollbar(list_fr, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")
        list_fr.rowconfigure(0, weight=1)
        list_fr.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_sel_count())

        log_fr = ttk.LabelFrame(self, text="Log")
        log_fr.pack(fill="both", **pad)
        self.log_text = tk.Text(log_fr, height=8, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

        self.status = ttk.Label(self, text="Gotowe", anchor="w")
        self.status.pack(fill="x", padx=8, pady=(0, 8))

    def _pick_output(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".html",
            filetypes=[("HTML", "*.html"), ("All", "*.*")],
            initialfile="confluence_export.html",
        )
        if path:
            self.output_var.set(path)

    def _log(self, msg: str) -> None:
        self._log_queue.put(msg)

    def _drain_log(self) -> None:
        try:
            while True:
                msg = self._log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(200, self._drain_log)

    def _set_busy(self, busy: bool, status: str = "") -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.btn_list.configure(state=state)
        self.btn_export.configure(state=state)
        if status:
            self.status.configure(text=status)

    def _fill_list(self, pages: list[tuple[str, str]]) -> None:
        self.pages = pages
        self.tree.delete(*self.tree.get_children())
        for i, (title, url) in enumerate(pages, 1):
            self.tree.insert("", "end", iid=str(i - 1), values=(i, title, url))
        n = len(pages)
        self.from_var.set("1" if n else "0")
        self.to_var.set(str(n))
        self._update_sel_count()
        self.status.configure(text=f"Lista: {n} stron")

    def _update_sel_count(self) -> None:
        self.sel_label.configure(text=f"Zaznaczono: {len(self.tree.selection())}")

    def _select_range(self) -> None:
        if not self.pages:
            return
        try:
            a = max(1, int(self.from_var.get()))
            b = min(len(self.pages), int(self.to_var.get()))
        except ValueError:
            messagebox.showerror("Blad", "Od/Do musza byc liczbami.")
            return
        if a > b:
            a, b = b, a
        self.tree.selection_set([str(i) for i in range(a - 1, b)])
        if self.tree.selection():
            self.tree.see(self.tree.selection()[0])
        self._update_sel_count()

    def _select_all(self) -> None:
        self.tree.selection_set(self.tree.get_children())
        self._update_sel_count()

    def _clear_sel(self) -> None:
        self.tree.selection_remove(self.tree.selection())
        self._update_sel_count()

    def _selected_urls(self) -> list[str]:
        urls: list[str] = []
        for iid in self.tree.selection():
            idx = int(iid)
            urls.append(self.pages[idx][1])
        return urls

    def _start_list(self) -> None:
        if self._busy:
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror("Blad", "Podaj URL.")
            return
        self._set_busy(True, "Pobieranie listy...")
        self._log("--- Pobieranie listy ---")

        def worker() -> None:
            try:
                pages = discover_pages(
                    url,
                    headed=self.headed_var.get(),
                    deep=self.deep_var.get(),
                    max_pages=0,
                    only_under=self.only_under_var.get(),
                    on_progress=self._log,
                )
                self.after(0, lambda: self._on_list_done(pages))
            except Exception as exc:
                self.after(0, lambda: self._on_error(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _on_list_done(self, pages: list[tuple[str, str]]) -> None:
        self._fill_list(pages)
        self._set_busy(False, f"Gotowe — {len(pages)} stron")
        self._log(f"Lista gotowa: {len(pages)} stron")
        if pages:
            self._select_all()

    def _start_export(self) -> None:
        if self._busy:
            return
        urls = self._selected_urls()
        if not urls:
            messagebox.showinfo(
                "Info",
                "Zaznacz strony na liscie albo uzyj 'Zaznacz zakres'.",
            )
            return
        out = Path(self.output_var.get().strip())
        start_url = self.url_var.get().strip()
        self._set_busy(True, f"Pobieranie {len(urls)} stron...")
        self._log(f"--- Eksport {len(urls)} stron -> {out} ---")

        def worker() -> None:
            try:
                scrape(
                    start_url=start_url,
                    output=out,
                    max_pages=0,
                    headed=self.headed_var.get(),
                    login_wait=300,
                    delay=1.0,
                    expand_tree=False,
                    fixed_urls=urls,
                    on_progress=self._log,
                )
                self.after(0, lambda: self._on_export_done(out, len(urls)))
            except Exception as exc:
                self.after(0, lambda: self._on_error(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _on_export_done(self, out: Path, n: int) -> None:
        self._set_busy(False, f"Zapisano {n} stron")
        self._log(f"Gotowe: {out.resolve()}")
        messagebox.showinfo("Gotowe", f"Zapisano {n} stron do:\n{out.resolve()}")

    def _on_error(self, exc: Exception) -> None:
        self._set_busy(False, "Blad")
        self._log(f"BLAD: {exc}")
        messagebox.showerror("Blad", str(exc))


def main() -> None:
    app = ConfluenceGui()
    app.mainloop()


if __name__ == "__main__":
    main()
