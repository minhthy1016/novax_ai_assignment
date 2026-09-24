# Walkthrough — kịch bản 12–13 phút

*Cho buổi review 29/09/2026. Đã tập trên `main` (`2c4cd13`) từ trạng thái sạch ngày 24/09: cả 6 mục đạt; chạy liền mạch `scripts/demo.sh --no-pause` mất khoảng 30 giây. Phần còn lại của thời gian là để nói.*

## Trước buổi review (15 phút trước giờ)

```bash
make reset && make up && make ingest     # trạng thái sạch, 11 tài liệu mẫu
make ui                                  # mở console dự phòng: http://localhost:8000/ui
scripts/demo.sh --no-pause > /dev/null   # chạy một lần để nạp sẵn các mô hình vào bộ nhớ
```

- [ ] `curl localhost:8000/readyz` trả về `"ready"`.
- [ ] Ollama đang chạy và đã có `llama3.2:3b` + `nomic-embed-text`.
- [ ] Đóng các ứng dụng nặng: máy 16 GB, mô hình cần RAM.
- [ ] Mở sẵn: terminal cỡ chữ lớn, `README.md`, `docs/decisions/`, và file `docs/walkthrough-recording.txt` để dự phòng.
- [ ] **Nếu PR #15 đã merge:** ở mục 3 dùng thêm câu ví dụ nguyên văn của đề bài (xem bên dưới).

## Kịch bản

Chạy `scripts/demo.sh`. Script dừng trước mỗi bước, bấm Enter để đi tiếp.

| Thời gian | Mục | Nói gì (ý chính) | Chỉ vào đâu trên màn hình |
|---|---|---|---|
| 0:00–1:30 | Mở đầu | Trợ lý nội bộ làm 2 việc: trả lời từ tài liệu được phép đọc, và thực hiện thao tác có kiểm soát. **Nguyên tắc: mô hình đề xuất, backend quyết định.** Phân quyền, cách ly, phê duyệt và audit nằm trong code và database, không nằm trong prompt. | Sơ đồ kiến trúc trong README |
| 1:30–3:00 | **1 · RAG** | Câu trả lời chỉ dựa vào nguồn được truy xuất; trích dẫn có tài liệu, phiên bản và đoạn. Tìm kiếm lai (vector + BM25), chia tài liệu parent–child, đã đo trên 41 câu hỏi vàng. | `citations`: `KB-ENG-001 v1, ¶1–7` |
| 3:00–4:30 | **2 · Tool** | Router chọn tool và tham số có kiểu; **quyền được kiểm tra trước khi tool chạy**, không dựa vào mô hình. U003 không có `server:read` nên bị từ chối, và không nhận được chút dữ liệu nào. | `status: ok` với dữ liệu, rồi `denied` |
| 4:30–7:00 | **3 · Thao tác nhạy cảm** *(trọng tâm)* | Yêu cầu chỉ tạo *đề xuất*. Ba lần duyệt sai đều bị chặn: thiếu quyền (2 lần) và **hash không khớp**, vì hash gắn chặt với đúng tham số của đề xuất. Duyệt đúng thì thực hiện **một lần**; duyệt lại bị chặn. Audit nối chuỗi hash và kiểm tra còn nguyên vẹn. | `pending` → 3 lần `denied` → `ok` → `already executed` → `intact: true` |
| 7:00–9:00 | **4 · Prompt injection** | Tài liệu độc hại **được** truy xuất, vì nó là dữ liệu hợp lệ trong kho. Nhưng nội dung truy xuất là dữ liệu không đáng tin: được escape và không có quyền ra lệnh. Nhờ tóm tắt thì chỉ phần thông tin hợp lệ được trả về, có trích dẫn. Biến thể dùng tool thì vẫn *pending*. | `UNTRUSTED TEXT…`; câu trả lời có `[1] KB-TEST-999`; `pending` |
| 9:00–10:30 | **5 · Provider lỗi** | Retry, fallback và timeout cho từng lần gọi. Circuit breaker mở thì lần sau bỏ qua ngay. Lỗi trả ra cho client có kiểm soát, kèm request ID, **không lộ lỗi gốc của provider**. | Danh sách `attempts`; `no_available_provider` |
| 10:30–12:00 | **6 · Cách ly** | U001 không nhận được gì, **kể cả tiêu đề** tài liệu HR. U004 có quyền thì thấy, nhưng **NIM và Claude bị bỏ qua**, vì tài liệu confidential không được rời máy. Cách ly có 2 lớp: SQL filter và Row-Level Security. | `[]`, rồi `KB-HR-002`, rồi `skipped:egress_not_permitted` |
| 12:00–13:00 | Kết | Đánh giá 71 ca, chạy sạch tái lập được: **63/71 chấm tự động (judge-v1), 66/71 chấm tay; cách ly, tool và từ chối đều 100%**. Nếu được hỏi về prompt: đã tự phát hiện prompt router và judge bị trùng câu với bộ đánh giá, đã gỡ bỏ và đo lại (D-34, PR #15). Đề xuất mở rộng AWS có mô hình tính công suất, chưa load test. Hạn chế nói thẳng: mô hình 3B là mức sàn, chưa có SSO thật. | `docs/trinh-bay-hoi-dong.md` §4–6 |

**Nếu còn thời gian, hoặc khi được hỏi về prompt (1 phút).** Mở bảng "Phát hiện: prompt bị học tủ" trong `docs/trinh-bay-hoi-dong.md` §4. Ba ý:
1. Đã tự phát hiện ví dụ trong prompt trùng với bộ đánh giá (router 10 câu, judge 3 đáp án), và đã gỡ bỏ.
2. Hệ thống **không** tốt lên: chọn tool 30/30 → 32/34, và một yêu cầu tạo ticket không ai yêu cầu. Đó là con số thật.
3. **Bộ đo mạnh lên rõ:** có test chống học tủ, judge khớp nhãn tay 36/39, 3 bên chấm độc lập, mã hash prompt trong mọi báo cáo.

**Mục 3, câu ví dụ của đề bài.** Chỉ dùng khi PR #15 đã merge: trên `main` hiện tại router vẫn từ chối nhầm câu này.

```bash
chat U005 '{"message":"Create an OpenVPN profile for employee John Tan - with approval"}' \
  | jq '{route, status: .tool.status, message: .tool.message}'     # → tool / pending
```

## Nếu có sự cố

| Sự cố | Làm gì |
|---|---|
| Một lệnh chậm (> 10 s) | Nói tiếp phần giải thích trong lúc chờ. Mô hình local chạy lần đầu có thể mất 4–5 s. |
| Mô hình trả lời lệch lời thoại (thường ở mục 4) | Nói rõ: *mô hình 3B diễn đạt khác nhau giữa các lần; điều được bảo đảm là không hành động, không lộ gì, và có trích dẫn*. Đây chính là lý do an toàn không đặt vào prompt. |
| API lỗi hoặc Ollama treo | `make down && make up`. Nếu vẫn lỗi thì mở `docs/walkthrough-recording.txt` (kết quả thật của lần tập) và giải thích trên đó. |
| Hội đồng muốn tự thử một câu | Dùng console (`make ui`): chọn người dùng, hỏi, xem badge `route`, trích dẫn và danh sách attempts. |
| **Không dùng NIM để demo** | Free tier mất 13–80 s mỗi câu. Chỉ nhắc rằng nó có trong gateway; mục 6 cho thấy nó bị bỏ qua đúng lúc. |

## Câu hỏi thường gặp

Xem [`walkthrough-qa-vi.html`](walkthrough-qa-vi.html) (tiếng Việt) và [`walkthrough-qa-en.html`](walkthrough-qa-en.html) (English).
