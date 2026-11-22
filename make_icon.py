"""生成应用图标 assets/app.ico（圆角石墨底 + 绿色下载箭头 + 进度托盘）。

用法：python make_icon.py
仅首次或改设计时运行；产物已被 build.py 引用，打包时自动带上。
"""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "assets", "app.ico")

# 主题色与 style-graphite.css 一致
BG = (32, 36, 43, 255)        # 石墨深底 #20242B
FG = (67, 207, 124, 255)      # 下载绿 #43CF7C
FG_DIM = (67, 207, 124, 90)   # 托盘淡绿

S = 1024  # 画布尺寸（先大后缩，保证小尺寸锐利）
RADIUS = 210


def rounded(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def build_once(size):
    """绘制 size x size 的 RGBA 图像。"""
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 背景：圆角方块
    rounded(d, (24, 24, S - 24, S - 24), RADIUS, BG)
    # 下载箭头：竖杆 + 三角头
    cx = S // 2
    stem_w = 130
    d.rounded_rectangle((cx - stem_w // 2, 260, cx + stem_w // 2, 520),
                        radius=60, fill=FG)
    d.polygon([(cx - 200, 500), (cx + 200, 500), (cx, 730)], fill=FG)
    # 进度托盘：横杆（淡绿）+ 左侧已填充段（亮绿）
    rounded(d, (250, 796, S - 250, 856), 28, FG_DIM)
    rounded(d, (250, 796, 600, 856), 28, FG)
    if size != S:
        img = img.resize((size, size), Image.LANCZOS)
    return img


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    # PIL 的 ICO 插件不用 append_images：以最大尺寸图为基底，
    # 传 sizes 让它自动降采样生成各档帧
    base = build_once(256)
    base.save(OUT, format="ICO", sizes=[(s, s) for s in sizes])
    print("已生成", OUT, f"({os.path.getsize(OUT)//1024} KB, 含 {len(sizes)} 档尺寸)")
    # macOS 需要 icns；Linux 桌面文件需要 png
    try:
        icns = os.path.join(os.path.dirname(OUT), "app.icns")
        build_once(512).save(icns, format="ICNS")
        png = os.path.join(os.path.dirname(OUT), "app_256.png")
        build_once(256).save(png, format="PNG")
        print("已生成 macOS icns 与 Linux png 图标")
    except Exception as e:
        print("[warn] icns/png 生成失败（不影响 Windows 打包）：", e)


if __name__ == "__main__":
    main()
