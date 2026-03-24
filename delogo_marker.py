#!/usr/bin/env python3
"""
Delogo Region Marker - 영상 프레임에서 텍스트 영역을 시각적으로 지정하는 GUI 도구
ffmpeg delogo 필터용 좌표를 JSON으로 출력
"""

import json
import subprocess
import tempfile
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from PIL import Image, ImageTk

# 상수
IMAGE_DIR = Path("/tmp/product_frames")
OUTPUT_JSON = IMAGE_DIR / "delogo_regions.json"
ORIGINAL_W, ORIGINAL_H = 1920, 1080
CANVAS_W = 1100
MIN_SHAPE_SIZE = 5

COLORS = {
    "box": {"outline": "#FF3333", "fill": "#FF3333"},
    "ellipse": {"outline": "#3366FF", "fill": "#3366FF"},
    "freehand": {"outline": "#33CC33", "fill": "#33CC33"},
}

TOOL_NAMES = {"box": "Box (사각형)", "ellipse": "Ellipse (타원)", "freehand": "Freehand (프리핸드)"}


class DelogoMarkerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Delogo Region Marker")

        self.image_dir = IMAGE_DIR
        self.output_json_path = OUTPUT_JSON

        # 이미지 목록 로드
        self.image_files = sorted(self.image_dir.glob("*.png"))
        if not self.image_files:
            messagebox.showerror("오류", f"이미지가 없습니다: {self.image_dir}")
            root.destroy()
            return

        self.current_index = 0
        self.shapes = {}  # key: image stem, value: list of shape dicts
        self.modified = False

        # 스케일 계산
        self.scale_factor = CANVAS_W / ORIGINAL_W
        self.canvas_h = int(ORIGINAL_H * self.scale_factor)

        # 그리기 상태
        self.current_tool = tk.StringVar(value="box")
        self.draw_start = None
        self.temp_item = None
        self.freehand_points = []
        self.freehand_items = []

        # 패더(padding) 값
        self.padding_var = tk.IntVar(value=10)

        # 프리뷰 모드
        self.preview_mode = False
        self._preview_photo_ref = None

        # 현재 표시 중인 PhotoImage 참조 유지
        self._photo_ref = None

        self._setup_ui()
        self._load_json()
        self._load_and_display_image(0)
        self._bind_shortcuts()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI 구성 ──────────────────────────────────────────

    def _setup_ui(self):
        self.root.configure(bg="#2B2B2B")

        # 메인 프레임
        main = tk.Frame(self.root, bg="#2B2B2B")
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # 상단: 캔버스 + 도구 패널
        top = tk.Frame(main, bg="#2B2B2B")
        top.pack(fill=tk.BOTH, expand=True)

        self._create_canvas(top)
        self._create_toolbar(top)

        # 중단: 네비게이션
        self._create_navigation(main)

        # 하단: 이미지 목록
        self._create_image_list(main)

    def _create_canvas(self, parent):
        frame = tk.Frame(parent, bg="#1E1E1E", bd=2, relief=tk.SUNKEN)
        frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(frame, width=CANVAS_W, height=self.canvas_h,
                                bg="#1E1E1E", highlightthickness=0, cursor="crosshair")
        self.canvas.pack()

        self.canvas.bind("<ButtonPress-1>", self._on_mouse_down)
        self.canvas.bind("<B1-Motion>", self._on_mouse_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_mouse_up)

    def _create_toolbar(self, parent):
        panel = tk.Frame(parent, bg="#333333", width=200, padx=10, pady=10)
        panel.pack(side=tk.RIGHT, fill=tk.Y)
        panel.pack_propagate(False)

        # 도구 선택
        tk.Label(panel, text="도구", font=("Helvetica", 13, "bold"),
                 fg="white", bg="#333333").pack(anchor=tk.W, pady=(0, 5))

        for tool_id, tool_name in TOOL_NAMES.items():
            color = COLORS[tool_id]["outline"]
            rb = tk.Radiobutton(panel, text=tool_name, variable=self.current_tool,
                                value=tool_id, fg=color, bg="#333333",
                                selectcolor="#444444", activebackground="#444444",
                                activeforeground=color, font=("Helvetica", 12))
            rb.pack(anchor=tk.W, pady=2)

        ttk.Separator(panel, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)

        # 버튼
        btn_style = {"font": ("Helvetica", 12), "width": 16}
        tk.Button(panel, text="↩ Undo (⌘Z)", command=self._undo_last_shape,
                  **btn_style).pack(pady=3)
        tk.Button(panel, text="✕ Clear All", command=self._clear_shapes,
                  **btn_style).pack(pady=3)

        ttk.Separator(panel, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)

        # 패딩(페더) 값
        tk.Label(panel, text="Padding (px)", font=("Helvetica", 12, "bold"),
                 fg="white", bg="#333333").pack(anchor=tk.W, pady=(0, 3))

        pad_frame = tk.Frame(panel, bg="#333333")
        pad_frame.pack(fill=tk.X, pady=(0, 5))

        self.padding_slider = tk.Scale(pad_frame, from_=0, to=100,
                                        orient=tk.HORIZONTAL, variable=self.padding_var,
                                        bg="#333333", fg="white", highlightthickness=0,
                                        troughcolor="#555555", length=120,
                                        command=self._on_padding_change)
        self.padding_slider.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.padding_entry = tk.Entry(pad_frame, textvariable=self.padding_var,
                                       width=4, font=("Courier", 12),
                                       bg="#444444", fg="white", justify=tk.CENTER)
        self.padding_entry.pack(side=tk.RIGHT, padx=(5, 0))

        ttk.Separator(panel, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)

        # 프리뷰 + 저장 버튼
        self.preview_btn = tk.Button(panel, text="👁 Preview (P)",
                                      command=self._toggle_preview,
                                      font=("Helvetica", 12, "bold"), width=16,
                                      fg="#FFAA00")
        self.preview_btn.pack(pady=3)

        self.reset_btn = tk.Button(panel, text="🔄 Reset This (R)",
                                    command=self._reset_current,
                                    font=("Helvetica", 12, "bold"), width=16,
                                    fg="#FF6666")
        self.reset_btn.pack(pady=3)

        tk.Button(panel, text="💾 Save JSON (⌘S)", command=self._save_json,
                  font=("Helvetica", 12, "bold"), width=16, fg="#33CC33").pack(pady=3)

        ttk.Separator(panel, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)

        # 도형 목록
        tk.Label(panel, text="Shapes", font=("Helvetica", 13, "bold"),
                 fg="white", bg="#333333").pack(anchor=tk.W, pady=(0, 5))

        self.shapes_listbox = tk.Listbox(panel, bg="#2B2B2B", fg="white",
                                          font=("Courier", 11), height=8,
                                          selectbackground="#555555")
        self.shapes_listbox.pack(fill=tk.BOTH, expand=True)

        ttk.Separator(panel, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)

        # delogo 결과 표시
        self.delogo_label = tk.Label(panel, text="delogo: --", font=("Courier", 10),
                                     fg="#AAAAAA", bg="#333333", wraplength=180,
                                     justify=tk.LEFT)
        self.delogo_label.pack(anchor=tk.W)

    def _create_navigation(self, parent):
        nav = tk.Frame(parent, bg="#2B2B2B", pady=8)
        nav.pack(fill=tk.X)

        tk.Button(nav, text="◀ Prev", command=self._prev_image,
                  font=("Helvetica", 12), width=8).pack(side=tk.LEFT, padx=5)

        self.nav_label = tk.Label(nav, text="", font=("Helvetica", 13, "bold"),
                                  fg="white", bg="#2B2B2B")
        self.nav_label.pack(side=tk.LEFT, expand=True)

        tk.Button(nav, text="Next ▶", command=self._next_image,
                  font=("Helvetica", 12), width=8).pack(side=tk.RIGHT, padx=5)

    def _create_image_list(self, parent):
        frame = tk.Frame(parent, bg="#2B2B2B")
        frame.pack(fill=tk.X, pady=(5, 0))

        scrollbar = tk.Scrollbar(frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.image_listbox = tk.Listbox(frame, bg="#2B2B2B", fg="#CCCCCC",
                                         font=("Courier", 11), height=6,
                                         selectbackground="#555555",
                                         yscrollcommand=scrollbar.set)
        self.image_listbox.pack(fill=tk.X)
        scrollbar.config(command=self.image_listbox.yview)

        for f in self.image_files:
            self.image_listbox.insert(tk.END, f"  {f.stem}")

        self.image_listbox.bind("<<ListboxSelect>>", self._on_list_select)

    # ── 키보드 단축키 ────────────────────────────────────

    def _bind_shortcuts(self):
        self.root.bind("<Left>", lambda e: self._prev_image())
        self.root.bind("<Right>", lambda e: self._next_image())
        self.root.bind("<Command-z>", lambda e: self._undo_last_shape())
        self.root.bind("<Command-s>", lambda e: self._save_json())
        self.root.bind("1", lambda e: self.current_tool.set("box"))
        self.root.bind("2", lambda e: self.current_tool.set("ellipse"))
        self.root.bind("3", lambda e: self.current_tool.set("freehand"))
        self.root.bind("p", lambda e: self._toggle_preview())
        self.root.bind("P", lambda e: self._toggle_preview())
        self.root.bind("r", lambda e: self._reset_current())
        self.root.bind("R", lambda e: self._reset_current())

    # ── 이미지 로드/표시 ─────────────────────────────────

    def _get_image_key(self, index):
        return self.image_files[index].stem

    def _load_and_display_image(self, index):
        self.current_index = index

        # 프리뷰 모드 해제
        if self.preview_mode:
            self.preview_mode = False
            self.preview_btn.config(text="👁 Preview (P)", relief=tk.RAISED)

        img = Image.open(self.image_files[index])
        img = img.resize((CANVAS_W, self.canvas_h), Image.LANCZOS)
        self._photo_ref = ImageTk.PhotoImage(img)

        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self._photo_ref)

        self._redraw_shapes()
        self._update_status()

    def _redraw_shapes(self):
        key = self._get_image_key(self.current_index)
        shapes_list = self.shapes.get(key, [])

        # 캔버스에서 도형만 삭제 (이미지 제외)
        self.canvas.delete("shape")

        for s in shapes_list:
            cx1, cy1 = self._original_to_canvas(s["x"], s["y"])
            cx2, cy2 = self._original_to_canvas(s["x"] + s["w"], s["y"] + s["h"])
            color = COLORS[s["type"]]

            if s["type"] == "box":
                self.canvas.create_rectangle(cx1, cy1, cx2, cy2,
                    outline=color["outline"], width=2,
                    fill=color["fill"], stipple="gray25", tags="shape")
            elif s["type"] == "ellipse":
                self.canvas.create_oval(cx1, cy1, cx2, cy2,
                    outline=color["outline"], width=2,
                    fill=color["fill"], stipple="gray25", tags="shape")
            elif s["type"] == "freehand":
                # 프리핸드는 바운딩 박스를 점선 사각형으로 표시
                self.canvas.create_rectangle(cx1, cy1, cx2, cy2,
                    outline=color["outline"], width=2, dash=(4, 4),
                    fill=color["fill"], stipple="gray12", tags="shape")
                # 포인트가 있으면 경로도 그리기
                points = s.get("points", [])
                if len(points) > 1:
                    canvas_points = []
                    for px, py in points:
                        cpx, cpy = self._original_to_canvas(px, py)
                        canvas_points.extend([cpx, cpy])
                    self.canvas.create_line(*canvas_points,
                        fill=color["outline"], width=2, tags="shape")

        # 합산 delogo 영역 표시
        if shapes_list:
            delogo = self._compute_combined_delogo(shapes_list)
            dx1, dy1 = self._original_to_canvas(delogo["x"], delogo["y"])
            dx2, dy2 = self._original_to_canvas(delogo["x"] + delogo["w"], delogo["y"] + delogo["h"])
            self.canvas.create_rectangle(dx1, dy1, dx2, dy2,
                outline="#FFFF00", width=1, dash=(6, 3), tags="shape")

    def _update_status(self):
        key = self._get_image_key(self.current_index)
        count = len(self.shapes.get(key, []))
        total = len(self.image_files)
        modified = " *" if self.modified else ""

        self.nav_label.config(
            text=f"{self.current_index + 1}/{total}  {key}  ({count} shapes){modified}")

        # 도형 목록 갱신
        self.shapes_listbox.delete(0, tk.END)
        for s in self.shapes.get(key, []):
            self.shapes_listbox.insert(tk.END,
                f"{s['type']:>8}  x={s['x']} y={s['y']} w={s['w']} h={s['h']}")

        # delogo 결과 (패딩 포함)
        shapes_list = self.shapes.get(key, [])
        if shapes_list:
            d = self._compute_combined_delogo(shapes_list)
            pad = self.padding_var.get()
            self.delogo_label.config(
                text=f"delogo: x={d['x']} y={d['y']}\n"
                     f"        w={d['w']} h={d['h']}\n"
                     f"padding: {pad}px")
        else:
            self.delogo_label.config(text="delogo: --")

        # 이미지 목록 완료 표시 갱신
        for i, f in enumerate(self.image_files):
            k = f.stem
            prefix = "● " if self.shapes.get(k) else "  "
            self.image_listbox.delete(i)
            self.image_listbox.insert(i, f"{prefix}{k}")
        self.image_listbox.selection_clear(0, tk.END)
        self.image_listbox.selection_set(self.current_index)
        self.image_listbox.see(self.current_index)

    # ── 좌표 변환 ────────────────────────────────────────

    def _canvas_to_original(self, cx, cy):
        ox = int(round(cx / self.scale_factor))
        oy = int(round(cy / self.scale_factor))
        return max(0, min(ox, ORIGINAL_W - 1)), max(0, min(oy, ORIGINAL_H - 1))

    def _original_to_canvas(self, ox, oy):
        return ox * self.scale_factor, oy * self.scale_factor

    # ── 마우스 이벤트 ────────────────────────────────────

    def _clamp_canvas(self, x, y):
        return max(0, min(x, CANVAS_W)), max(0, min(y, self.canvas_h))

    def _on_mouse_down(self, event):
        x, y = self._clamp_canvas(event.x, event.y)
        tool = self.current_tool.get()

        if tool in ("box", "ellipse"):
            self.draw_start = (x, y)
            color = COLORS[tool]
            if tool == "box":
                self.temp_item = self.canvas.create_rectangle(
                    x, y, x, y, outline=color["outline"], width=2, dash=(3, 3))
            else:
                self.temp_item = self.canvas.create_oval(
                    x, y, x, y, outline=color["outline"], width=2, dash=(3, 3))

        elif tool == "freehand":
            self.freehand_points = [(x, y)]
            self.freehand_items = []

    def _on_mouse_drag(self, event):
        x, y = self._clamp_canvas(event.x, event.y)
        tool = self.current_tool.get()

        if tool in ("box", "ellipse") and self.draw_start and self.temp_item:
            sx, sy = self.draw_start
            self.canvas.coords(self.temp_item, sx, sy, x, y)

        elif tool == "freehand" and self.freehand_points:
            px, py = self.freehand_points[-1]
            item = self.canvas.create_line(px, py, x, y,
                fill=COLORS["freehand"]["outline"], width=2)
            self.freehand_items.append(item)
            self.freehand_points.append((x, y))

    def _on_mouse_up(self, event):
        x, y = self._clamp_canvas(event.x, event.y)
        tool = self.current_tool.get()
        key = self._get_image_key(self.current_index)

        if key not in self.shapes:
            self.shapes[key] = []

        if tool in ("box", "ellipse") and self.draw_start:
            if self.temp_item:
                self.canvas.delete(self.temp_item)
                self.temp_item = None

            sx, sy = self.draw_start
            ox1, oy1 = self._canvas_to_original(min(sx, x), min(sy, y))
            ox2, oy2 = self._canvas_to_original(max(sx, x), max(sy, y))
            w, h = ox2 - ox1, oy2 - oy1

            if w >= MIN_SHAPE_SIZE and h >= MIN_SHAPE_SIZE:
                self.shapes[key].append({
                    "type": tool, "x": ox1, "y": oy1, "w": w, "h": h
                })
                self.modified = True

            self.draw_start = None

        elif tool == "freehand" and len(self.freehand_points) >= 3:
            for item in self.freehand_items:
                self.canvas.delete(item)
            self.freehand_items = []

            orig_points = [self._canvas_to_original(cx, cy)
                           for cx, cy in self.freehand_points]
            xs = [p[0] for p in orig_points]
            ys = [p[1] for p in orig_points]
            ox, oy = min(xs), min(ys)
            w, h = max(xs) - ox, max(ys) - oy

            if w >= MIN_SHAPE_SIZE and h >= MIN_SHAPE_SIZE:
                self.shapes[key].append({
                    "type": "freehand", "x": ox, "y": oy, "w": w, "h": h,
                    "points": orig_points
                })
                self.modified = True

            self.freehand_points = []

        self._redraw_shapes()
        self._update_status()

    # ── 액션 ─────────────────────────────────────────────

    def _undo_last_shape(self):
        key = self._get_image_key(self.current_index)
        if self.shapes.get(key):
            self.shapes[key].pop()
            self.modified = True
            self._redraw_shapes()
            self._update_status()

    def _clear_shapes(self):
        key = self._get_image_key(self.current_index)
        if self.shapes.get(key):
            if messagebox.askyesno("확인", f"'{key}'의 모든 도형을 삭제하시겠습니까?"):
                self.shapes[key] = []
                self.modified = True
                self._redraw_shapes()
                self._update_status()

    def _reset_current(self):
        key = self._get_image_key(self.current_index)
        self.shapes[key] = []
        self.modified = True
        if self.preview_mode:
            self.preview_mode = False
            self.preview_btn.config(text="👁 Preview (P)", relief=tk.RAISED)
        self._load_and_display_image(self.current_index)

    def _prev_image(self):
        if self.current_index > 0:
            self._load_and_display_image(self.current_index - 1)

    def _next_image(self):
        if self.current_index < len(self.image_files) - 1:
            self._load_and_display_image(self.current_index + 1)

    def _on_list_select(self, event):
        sel = self.image_listbox.curselection()
        if sel:
            self._load_and_display_image(sel[0])

    # ── JSON 저장/불러오기 ───────────────────────────────

    def _save_json(self):
        output = {"_settings": {"padding": self.padding_var.get()}}
        for key, shapes_list in self.shapes.items():
            if not shapes_list:
                continue
            entry = {
                "shapes": [],
                "delogo": self._compute_combined_delogo(shapes_list)
            }
            for s in shapes_list:
                shape_data = {"type": s["type"], "x": s["x"], "y": s["y"],
                              "w": s["w"], "h": s["h"]}
                if s["type"] == "freehand" and "points" in s:
                    shape_data["points"] = s["points"]
                entry["shapes"].append(shape_data)
            output[key] = entry

        with open(self.output_json_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        self.modified = False
        self._update_status()
        messagebox.showinfo("저장 완료", f"저장됨: {self.output_json_path}")

    def _load_json(self):
        if not self.output_json_path.exists():
            return
        with open(self.output_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 설정 로드
        settings = data.pop("_settings", {})
        if "padding" in settings:
            self.padding_var.set(settings["padding"])
        for key, entry in data.items():
            self.shapes[key] = []
            for s in entry.get("shapes", []):
                shape = {"type": s["type"], "x": s["x"], "y": s["y"],
                         "w": s["w"], "h": s["h"]}
                if "points" in s:
                    shape["points"] = s["points"]
                self.shapes[key].append(shape)

    def _compute_combined_delogo(self, shapes_list, with_padding=True):
        if not shapes_list:
            return None
        pad = self.padding_var.get() if with_padding else 0
        min_x = min(s["x"] for s in shapes_list) - pad
        min_y = min(s["y"] for s in shapes_list) - pad
        max_x = max(s["x"] + s["w"] for s in shapes_list) + pad
        max_y = max(s["y"] + s["h"] for s in shapes_list) + pad
        # 이미지 경계 클램핑
        min_x = max(0, min_x)
        min_y = max(0, min_y)
        max_x = min(ORIGINAL_W, max_x)
        max_y = min(ORIGINAL_H, max_y)
        return {"x": min_x, "y": min_y, "w": max_x - min_x, "h": max_y - min_y}

    # ── 프리뷰 ───────────────────────────────────────────

    def _on_padding_change(self, _=None):
        self._redraw_shapes()
        self._update_status()
        if self.preview_mode:
            self._show_preview()

    def _toggle_preview(self):
        key = self._get_image_key(self.current_index)
        shapes_list = self.shapes.get(key, [])

        if self.preview_mode:
            # 프리뷰 해제 → 원본으로 복귀
            self.preview_mode = False
            self.preview_btn.config(text="👁 Preview (P)", relief=tk.RAISED)
            self._load_and_display_image(self.current_index)
        elif shapes_list:
            # 프리뷰 실행
            self._show_preview()
        else:
            messagebox.showinfo("안내", "도형을 먼저 그려주세요.")

    def _show_preview(self):
        key = self._get_image_key(self.current_index)
        shapes_list = self.shapes.get(key, [])
        if not shapes_list:
            return

        delogo = self._compute_combined_delogo(shapes_list)
        src = str(self.image_files[self.current_index])

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name

        cmd = [
            "ffmpeg", "-y", "-i", src,
            "-vf", f"delogo=x={delogo['x']}:y={delogo['y']}:w={delogo['w']}:h={delogo['h']}",
            "-vframes", "1", "-update", "1", tmp_path
        ]

        try:
            subprocess.run(cmd, capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
            img = Image.open(tmp_path)
            img = img.resize((CANVAS_W, self.canvas_h), Image.LANCZOS)
            self._preview_photo_ref = ImageTk.PhotoImage(img)

            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor=tk.NW, image=self._preview_photo_ref)

            # 프리뷰 표시 라벨 (좌상단, 작게)
            self.canvas.create_text(8, 8, text="PREVIEW", anchor=tk.NW,
                                     fill="#FFAA00", font=("Helvetica", 11),
                                     tags="shape")

            self.preview_mode = True
            self.preview_btn.config(text="👁 Back to Edit (P)", relief=tk.SUNKEN)
        except Exception as e:
            messagebox.showerror("프리뷰 오류", str(e))
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    # ── 종료 처리 ────────────────────────────────────────

    def _on_close(self):
        if self.modified:
            if messagebox.askyesno("저장 확인", "변경사항이 있습니다. 저장하시겠습니까?"):
                self._save_json()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = DelogoMarkerApp(root)
    root.mainloop()
