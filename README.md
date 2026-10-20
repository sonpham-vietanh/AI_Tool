# AI Book Translator Hub (Multi-Agent Studio)

Hệ thống **Tìm kiếm Đa Phiên Bản (EPUB / PDF / DOCX / Bản Leak)** & **Dịch Thuật Sách Tự Động Đa Agent** giữ nguyên định dạng gốc.

## Tính năng cốt lõi

1. **Agent 1 — Săn lùng Đa Phiên Bản & Bản Leak (`backend/search/providers.py`):**
   - Tìm kiếm song song trên 5 nguồn: **Web/GitHub/Google Drive Leaks**, **Internet Archive / OpenLibrary Deep File Resolver**, **LibGen**, **Anna's Archive**, và **Project Gutenberg**.
   - Hỗ trợ tự động mở rộng tên sách tiếng Việt sang tên gốc tiếng Anh để săn đủ các lần tái bản (1st/2nd/3rd Edition, bản PDF/EPUB).
   - Bảng chọn ấn bản trực quan (Bước 1) trước khi đưa vào dây chuyền dịch thuật (Bước 2).
2. **Dây chuyền 5 Agent Dịch thuật (`backend/llm/` & `backend/translators/`):**
   - **Agent 2 (Lexicographer):** Trích xuất thuật ngữ & lập từ điển (Glossary) toàn sách.
   - **Agent 3 (Main Translator):** Dịch thuật song song đa luồng bám sát ngữ cảnh chương trước và Glossary.
   - **Agent 4 (Literary Editor):** Hiệu đính văn phong tiếng Việt mượt mà, tự nhiên.
   - **Agent 5 (Typesetter):** Đóng gói lại định dạng gốc (`EPUB`, `DOCX`, `PDF`, `TXT`) hoặc xuất bản song ngữ.

## Khởi chạy nhanh (Windows)

```bat
pip install -r requirements.txt
start_hub.bat
```

Hoặc chạy trực tiếp bằng Uvicorn:

```bash
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8088 --reload
```

Sau đó truy cập: **http://localhost:8088**
