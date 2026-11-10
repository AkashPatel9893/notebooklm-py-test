"""Local thumbnail generation does not require Drive or MongoDB."""
import importlib.util
from pathlib import Path

from PIL import Image

spec = importlib.util.spec_from_file_location("pipeline_thumbnails", Path(__file__).resolve().parents[2] / "pipeline" / "thumbnails.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def make_pdf(path, count):
    pages = [Image.new("RGB", (400, 300), (index * 30, 40, 80)) for index in range(count)]
    pages[0].save(path, "PDF", save_all=True, append_images=pages[1:])


def test_generates_lossless_pages_and_reuses_unchanged_files(tmp_path):
    make_pdf(tmp_path / "slides.pdf", 5)
    make_pdf(tmp_path / "source.pdf", 1)
    Image.new("RGBA", (40, 20), (10, 20, 30, 100)).save(tmp_path / "infographic.png")
    generated = module.generate(tmp_path)
    pages = [tmp_path / f"slides_page_{index}.webp" for index in range(1, 6)]
    assert all(page in generated and page.exists() for page in pages)
    assert not (tmp_path / "slides_page_6.webp").exists()
    assert (tmp_path / "chapter_thumbnail.webp").exists()
    assert (tmp_path / "podcast_thumbnail.webp").exists()
    with Image.open(tmp_path / "infographic_thumbnail.webp") as image:
        assert image.getpixel((0, 0)) == (10, 20, 30, 100)
    stamp = pages[0].stat().st_mtime_ns
    module.generate(tmp_path)
    assert pages[0].stat().st_mtime_ns == stamp
    make_pdf(tmp_path / "slides.pdf", 2)
    module.generate(tmp_path)
    assert all(not page.exists() for page in pages[2:])
    assert (tmp_path / "infographic.png").exists()


def _load_pipeline(name):
    import sys

    pipeline = Path(__file__).resolve().parents[2] / "pipeline"
    sys.path.insert(0, str(pipeline))
    try:
        loader = importlib.util.spec_from_file_location(f"pipeline_{name}", pipeline / f"{name}.py")
        loaded = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(loaded)
        return loaded
    finally:
        sys.path.remove(str(pipeline))


def test_mongo_items_carry_preview_links():
    mongo_sync = _load_pipeline("mongo_sync")

    def entry(file_id):
        return {"id": file_id, "md5": "abcdef1234567890", "preview_url": f"https://drive/{file_id}"}

    drive = {
        "source.pdf": entry("src"), "chapter_thumbnail.webp": entry("ch"),
        "slides.pdf": entry("sl"), "slides_page_2.webp": entry("p2"), "slides_page_1.webp": entry("p1"),
        "infographic.png": entry("inf"), "infographic_thumbnail.webp": entry("infw"),
        "video_thumbnail.gif": entry("gif"),
    }
    youtube = {"cinematic_video.mp4": {"id": "yt1", "embed_url": "e"}, "audio_video.mp4": {"id": "yt2", "embed_url": "a"}}
    drive["podcast_thumbnail.webp"] = entry("pod")
    items = {item["key"]: item for item in mongo_sync.build_items(drive, youtube)}
    assert items["source_pdf"]["thumbnail_url"] == "https://drive/ch"
    assert items["slides"]["thumbnail_url"] == "https://drive/p1"
    assert items["slides"]["thumbnail_urls"] == ["https://drive/p1", "https://drive/p2"]
    assert items["infographic"]["thumbnail_url"] == "https://drive/infw"
    assert items["audio"]["thumbnail_url"] == "https://drive/pod"
    video = items["cinematic_video"]
    assert video["thumbnail_url"] == "https://i.ytimg.com/vi/yt1/hqdefault.jpg"
    assert video["thumbnail_animation_url"] == "https://drive.google.com/uc?export=view&id=gif&v=abcdef123456"


def test_preview_helpers():
    mongo_sync = _load_pipeline("mongo_sync")
    drive = {f"slides_page_{n}.webp": {"preview_url": f"u{n}"} for n in (10, 2, 1)}
    assert mongo_sync._slide_pages(drive) == ["u1", "u2", "u10"]
    gif = mongo_sync._gif_link({"id": "g", "md5": "abcdef1234567890"})
    assert gif == "https://drive.google.com/uc?export=view&id=g&v=abcdef123456"
    assert mongo_sync._gif_link(None) == ""
