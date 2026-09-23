# OpsAssist — Trợ lý vận hành AI nội bộ

*Tài liệu trình bày cho hội đồng · cập nhật 24/09/2026 (ngày 6/6) · review 29/09/2026*

---

## 1. Dự án là gì?

OpsAssist là trợ lý nội bộ cho nhân viên, làm **hai việc**:

1. **Trả lời câu hỏi từ tài liệu công ty** — chỉ dùng tài liệu *người hỏi được phép đọc*, và luôn **kèm trích dẫn** (tài liệu, phiên bản, mục, đoạn).
2. **Thực hiện một số thao tác vận hành đã được duyệt** — xem trạng thái server, tạo/xem ticket, yêu cầu cấp VPN (VPN cần **hai người**: một người yêu cầu, một người duyệt).

**Nguyên tắc cốt lõi:** *mô hình đề xuất, backend quyết định.*
Xác thực, phân quyền, cách ly dữ liệu, phê duyệt và nhật ký kiểm toán đều được thực thi **trong code và trong database** — không bao giờ dựa vào việc "dặn" mô hình qua prompt.

### Ví dụ nhanh

| Người hỏi | Câu hỏi | Kết quả |
|---|---|---|
| Aisha (Engineering) | "Khung giờ deploy production là khi nào?" | Trả lời + trích dẫn `KB-ENG-001` |
| Aisha (Engineering) | "Lương thưởng HR năm nay thế nào?" | Không thấy gì — tài liệu HR bị chặn ở tầng database |
| Mei Lin (HR, không có quyền server) | "Kiểm tra server api-01" | Bị từ chối **trước khi** tool chạy |
| Priya (IT, có `vpn:create`) | "Tạo VPN cho tôi" | Chỉ tạo *đề xuất*, chờ người có `vpn:approve` duyệt |

---

## 2. Kiến trúc

```mermaid
flowchart TB
  client["Client<br/>Console UI · curl · Flowise"]
  subgraph api["OpsAssist API (FastAPI)"]
    direction LR
    authz["Xác thực + Chính sách<br/>bạn là ai, được xem gì"]
    agent["Điều phối (LangGraph)<br/>kiến thức · tool · từ chối"]
    tools["Kiểm soát tool<br/>schema · phê duyệt"]
  end
  kb[("Kho tri thức<br/>Postgres + pgvector<br/>Row-Level Security")]
  ops[("Hệ thống vận hành<br/>server · ticket · VPN")]
  llm{{"LLM Gateway<br/>NVIDIA NIM · Ollama · Claude · mock"}}
  audit[("Audit + metrics<br/>log nối chuỗi hash")]
  worker["Worker nạp tài liệu<br/>parse · chunk · embed"]

  client --> api
  agent --> kb
  tools --> ops
  agent --> llm
  api --> audit
  worker --> kb
  client -. "upload" .-> worker
```

### Luồng một câu hỏi (5 bước)

1. **Xác thực**: JWT gắn với user + role; quyền luôn đọc lại từ database.
2. **Định tuyến**: một mô hình nhỏ chạy local phân loại câu hỏi → *chào hỏi / tra cứu kiến thức / gọi tool / từ chối*.
3. **Tra cứu**: tìm kiếm lai (vector + full-text) **đã lọc theo phòng ban và mức mật** — lọc 2 lớp: câu SQL **và** Row-Level Security của Postgres.
4. **Sinh câu trả lời**: qua Gateway (có retry, fallback, circuit breaker). Tài liệu *confidential* chỉ được gửi tới mô hình chạy nội bộ, không ra ngoài.
5. **Kiểm tra trích dẫn + ghi audit**: trích dẫn nào không trỏ tới nguồn đã truy xuất thì bị loại; mọi hành động ghi vào log nối chuỗi hash (không sửa/xoá được).

### Các quyết định lớn

| Vấn đề | Chọn | Thay vì | Lý do |
|---|---|---|---|
| Lưu vector | pgvector trong Postgres chính | Qdrant, Weaviate | Quyền, phiên bản, vector nằm chung một transaction; RLS bảo vệ luôn cả truy xuất |
| Cách ly phòng ban | Lọc SQL **+** RLS | Chỉ lọc trong code | Quên lọc ở code thì DB vẫn trả về rỗng |
| Chia nhỏ tài liệu | Parent-child (khớp đoạn nhỏ, đưa cả mục cho mô hình) | Theo trang, cửa sổ cố định, Docling | **Đo được**: tốt nhất trên bộ 41 câu hỏi vàng |
| Điều phối | LangGraph + checkpoint Postgres | Tự viết state machine | Tạm dừng chờ phê duyệt VPN, sống sót qua restart |
| Gọi mô hình | Gateway tự xây | LiteLLM | Fallback, luật dữ liệu ra ngoài, đếm token — tự kiểm soát và giải thích được |
| Mô hình định tuyến | Mô hình nhỏ local riêng | Dùng chung mô hình trả lời | **Đo được**: NIM timeout 60s → mọi lượt âm thầm rơi về chế độ tra cứu |

Toàn bộ **35 quyết định** (kèm phương án đã cân nhắc và số đo) nằm trong `docs/decisions/`.

---

## 3. Cấu trúc code

```
src/opsassist/
├── main.py, config.py        # khởi tạo app, cấu hình
├── auth.py, middleware.py    # JWT, request ID, logging
├── ratelimit.py              # giới hạn tần suất theo người gọi (token bucket)
├── gateway/                  # LLM Gateway: định tuyến, retry, fallback, circuit breaker
├── providers/                # adapter: NIM (OpenAI-compatible), Ollama, Claude, mock
├── knowledge/                # tri thức: parsing → chunking → ingest → retrieval → upload
├── rag.py                    # dựng prompt, kiểm tra trích dẫn, từ chối khi không có nguồn
├── agent/                    # LangGraph: đồ thị định tuyến, checkpoint, dịch vụ agent
├── tools/                    # 5 tool có schema kiểu chặt + bộ thực thi
├── policy/                   # quyền truy cập (access.py) + audit nối chuỗi hash
├── memory.py                 # bộ nhớ lâu dài: chỉ lưu các khóa trong allowlist
├── api/                      # các endpoint: chat, search, actions, tickets, memory, audit...
├── db/                       # SQLAlchemy models, session
└── worker.py                 # worker Dramatiq nạp tài liệu (retry + dead-letter)

migrations/        # 8 migration Alembic (gồm role DB không phải superuser, RLS)
sample_data/       # 11 tài liệu mẫu (md, txt, pdf) + 6 nhân viên mẫu
evaluation/        # bộ đánh giá 71 ca + LLM judge + so sánh chiến lược chunking
tests/             # unit + integration
web/index.html     # console thử nghiệm (chỉ dùng dev)
docs/              # quyết định kiến trúc (D-xx), bảng truy vết yêu cầu
```

**Stack:** Python 3.12 · FastAPI · Postgres + pgvector · Redis · Dramatiq · LangGraph · Docker Compose.
**Mô hình:** NVIDIA NIM `gpt-oss-20b` (mặc định) · Ollama `llama3.2:3b` (local) · Claude (tắt tới khi có API key) · mock (cho CI). Embedding: `nomic-embed-text` chạy local.

---

## 4. Cách chạy

```bash
cp .env.example .env                        # cấu hình mặc định, không cần API key
ollama pull nomic-embed-text llama3.2:3b    # mô hình local
make up                                     # build, migrate, seed, chờ healthy
make ingest                                 # nạp tài liệu mẫu
make ui                                     # mở console: http://localhost:8000/ui
```

| Lệnh | Việc |
|---|---|
| `make test` | Unit test, không cần dịch vụ |
| `make test-integration` | Integration test trên stack đang chạy |
| `make test-security` | Test bảo mật: phân quyền, cách ly, injection, audit, egress |
| `make eval` | Bộ đánh giá 71 ca có LLM judge (cần `ollama pull qwen2.5:7b`) |
| `make eval-control` | Như trên + đường cơ sở "không truy xuất" để so sánh |

---

## 5. Kết quả đến hiện tại

### Tiến độ theo ngày

| Ngày | Nội dung | Trạng thái |
|---|---|---|
| D1 | Nền tảng: API, DB, health check, CI | ✅ đã merge |
| D2 | Task 1 — LLM Gateway, nhiều provider, fallback | ✅ PR #1 |
| D3 | Task 2 — RAG có cách ly phòng ban + đánh giá chunking | ✅ PR #2–#5 |
| D4 | Task 3+4 — Tool, phê duyệt 2 người, audit, bộ nhớ, upload, rate limit | ✅ PR #6 |
| D5 | Task 5 — Bộ đánh giá 71 ca, LLM judge, PDF bố cục phức tạp; sửa đọc bảng PDF, trích dẫn, trả lời một phần, gán nguồn | ✅ PR #7, #8, #9 đã merge; chạy lại sạch từ `main`, kết quả cuối đã chốt |
| D6 | Đề xuất mở rộng trên AWS (D-60) có mô hình tính công suất; chuẩn bị trình bày | ✅ đề xuất (PR #11); 🟡 chuẩn bị walkthrough |

### Kiểm thử

- **186 unit test + 63 integration test** đều pass; lint + mypy (strict) sạch. (Kết quả đánh giá D5 bên dưới được chạy trên commit `7c4236f`, lúc đó là 184 unit test.)

### Đánh giá 71 ca — lần chạy cuối, sạch, tái lập được

Chạy từ `main` (`7c4236f`) ở trạng thái sạch: xoá database, build lại image, nạp lại 11 tài liệu mẫu, rồi chạy đủ 71 ca (trả lời bằng `llama3.2:3b`, chấm bằng `qwen2.5:7b`). Cùng commit đó: lint sạch, 184 unit test và 63 integration test đều pass.

```text
71-case evaluation — clean reproducible run

Strict correctness:        63/71 (88.7%)
Manual review:              66/71 (93.0%)

Isolation:                  10/10
Tool accuracy:              30/30
Abstention:                 12/12
Citation validity:          36/36
Exact citation support:     35/35
```

| Tiêu chí | Kết quả |
|---|---|
| **Cách ly phòng ban** | **10/10** — không rò rỉ tài liệu nào |
| **Độ chính xác tool** (chọn đúng tool, tham số, phân quyền) | **30/30** |
| **Từ chối đúng lúc** (không có nguồn / không có quyền) | **12/12** |
| Trích dẫn hợp lệ (trỏ đúng nguồn đã truy xuất) | 36/36 |
| Trích dẫn thật sự hỗ trợ câu văn (LLM judge chấm từng trích dẫn) | **35/35** |
| Trích dẫn đúng tài liệu mong đợi (câu trả lời không có trích dẫn tính là sai) | 36/37 |
| Dữ kiện tham chiếu được nêu đúng | 34/39 = 0.87 |
| Trả lời đúng hoàn toàn | **63/71 = 88,7%** (chấm tay: **66/71 = 93,0%**) |
| Backend tự sửa trích dẫn sai nguồn | 3 câu trả lời, 4 nguồn — cả 4 đều được judge xác nhận đúng |
| Truy xuất: Hit@1 · MRR (41 câu vàng, tìm kiếm lai) | 0.927 · 0.963 |
| Độ trễ p50 / p95 (mô hình 3B chạy trên laptop) | 1,1 s / 4,5 s |

**So với mô hình không có RAG** (cùng mô hình, không truy xuất, không phân quyền): chỉ đúng **16%** dữ kiện (so với **87%** qua hệ thống), không có trích dẫn, và trả lời **5/5** câu mà người hỏi không có quyền hỏi.

**Điểm quan trọng:** mọi tiêu chí *an toàn* (cách ly, phân quyền, phê duyệt) đạt 100% và được chấm **bằng luật, không dùng mô hình**. LLM judge chỉ chấm chất lượng câu văn.

**Vì sao "chấm tay" cao hơn?** Điểm 63/71 do code đánh giá tự tính, không chỉnh tay ca nào. Đọc từng ca trong 8 ca trượt:
- **5 lỗi thật:** E12 (tóm tắt đúng nhưng không có trích dẫn), K08 (thêm câu "không được nêu" mâu thuẫn với câu trả lời đúng), M02 (nói "nguồn không đề cập thứ Sáu" thay vì đính chính lịch deploy là thứ Ba/thứ Năm), M01 và M04 (từ chối an toàn thay vì đính chính tiền đề sai).
- **2 lỗi của judge:** M03, M05 — câu trả lời đúng nhưng judge chấm sai.
- **1 bộ lọc bắt nhầm:** E09 — câu từ chối đúng có chứa cụm "system prompt" nằm trong danh sách cấm.

Mọi ca trượt đều in nguyên câu trả lời để người đọc tự kiểm tra.

### Bốn lỗi đã sửa hôm nay (ví dụ về cách làm việc)

**1. Đọc nhầm dòng bảng trong PDF (ca L04).**
- Hỏi "Tier 2 phải phản hồi SEV1 trong bao lâu?" → trả lời *30 phút* (sai, đúng là *10 phút*). Bảng trong PDF bị trích thành chuỗi phẳng, mô hình 3B đọc nhầm dòng.
- Sửa: parser đọc toạ độ ô trong PDF, viết lại mỗi dòng kèm nhãn cột: `Tier 2 - platform (SEV1): Hours = 24/7; Acknowledge within = 10 minutes; ...`
- Kết quả: đúng 3/3 lần; câu hỏi đối chứng về dòng bên cạnh vẫn đúng (30 phút); truy xuất không giảm (Hit@1 0.878 → 0.927).

**2. Câu trả lời đúng nhưng không có trích dẫn** (phát hiện khi thử vai HR trên console).
- Mô hình viết "according to source 1" thay vì `[1]`, nên hệ thống không nhận ra trích dẫn.
- Sửa: tự chuyển "source 1" → `[1]`; console hiện nhãn đỏ **uncited** nếu vẫn không có trích dẫn.
- Bộ đánh giá cũng có đúng điểm mù này (câu không trích dẫn vẫn được tính đạt) → đã sửa, giờ tính là trượt.

**3. Câu hỏi hai phần bị từ chối cả câu.**
- "Nhân viên có bao nhiêu ngày phép năm và phép ốm?" → từ chối, vì tài liệu không có chính sách phép ốm.
- Thử 4 cách viết prompt trên dữ liệu thật; chọn cách cho kết quả đúng 5/6: *"14 ngày phép năm [1]. Tài liệu không đề cập phép ốm."*
- **Cái giá:** mô hình 3B đôi khi thêm câu "nguồn không đề cập…" vào câu hỏi đã trả lời đủ. Đã ghi rõ trong báo cáo, không giấu.

**4. Dữ kiện đúng nhưng gán nhầm nguồn (ca L03).**
- Hỏi "Khi nào payment-worker được scale?" → trả lời đúng (*hàng đợi trên 5.000*) nhưng trích dẫn ghi chú sự cố, trong khi chỉ bảng giá năng lực [1] có con số 5.000.
- Sửa: sau khi mô hình trả lời, backend so các con số trong từng câu với nguồn được trích; nếu con số không có trong nguồn đó mà có trong nguồn khác đã truy xuất → chuyển trích dẫn sang nguồn đúng. Không cần gọi thêm mô hình, và báo cáo đếm số lần chỉnh để không có sửa "ngầm".
- Kết quả: L03 trích đúng nguồn 6/6 lần; các câu khác không bị đụng tới.

### Hạn chế — nói thẳng (phương pháp đã đóng băng, các điểm này được ghi nhận chứ không tối ưu thêm)

- **Gán nguồn chỉ được kiểm tra với câu có con số:** câu không có số vẫn có thể bị gán nhầm nguồn (vẫn là nguồn người dùng được phép xem — không rò rỉ).
- **Câu "nguồn không đề cập…" thừa:** thường vô hại, nhưng ở K08 nó sai (nói nguồn không đề cập điều mà câu trước vừa trả lời).
- **Câu trả lời có thể thiếu trích dẫn (E12):** console gắn nhãn **uncited** và bộ đánh giá tính là trượt, nhưng hệ thống chưa chặn.
- **Tiền đề sai (M01, M02, M04):** đôi khi từ chối hoặc chỉ nói "không đề cập" thay vì đính chính ("sự cố kéo dài 3 giờ…" → nên trả lời "thực tế là 18 phút").
- **Mô hình 3B là mức sàn:** các con số trên là cận dưới; gateway có thể chuyển sang mô hình lớn hơn mà không đổi code.
- **SSO thật chưa có:** dùng bộ phát token dev thay cho IdP công ty.
- **AWS mới thiết kế, chưa triển khai;** số liệu mở rộng suy ra từ đo đạc thực tế, chưa load test.

---

## 6. Đề xuất mở rộng (AWS) — tóm tắt

Mục tiêu (theo đề bài): **5.000 nhân viên, 1 triệu tài liệu, 100 request AI đồng thời**, nhiều phòng ban, nhiều provider, có cụm GPU. Hạ tầng: **AWS**.

**Nguyên tắc:** kiến trúc giữ nguyên, chỉ tách từng tầng để scale độc lập. Các quy tắc đang đúng hôm nay vẫn giữ: cách ly ở tầng lưu trữ, *mô hình đề xuất – backend quyết định*, dữ liệu confidential không rời VPC.

### Con số không gõ tay — sinh ra từ mô hình tính công suất

Mọi con số dưới đây do `evaluation/capacity.py` tính (`make capacity`), mỗi đầu vào ghi rõ **đo được** hay **giả định**. Nếu tài liệu D-60 ghi một con số mà mô hình không còn cho ra, unit test sẽ báo lỗi.

| Đầu vào | Giá trị | Nguồn |
|---|---|---|
| Số chunk trên 1.000 token | 18,7 | **đo** bằng chunker thật trên dữ liệu mẫu |
| Token prompt mỗi câu trả lời | 1.100 | **đo** (p95 của lần chạy D5 cuối) |
| Tốc độ embedding | 164 chunk/s trên laptop | **đo** |
| Độ dài tài liệu trung bình | 2.500 token (~5 trang) | giả định |
| Token đầu ra mỗi câu trả lời | 300 | giả định (mô hình ~20B) |
| Thông lượng GPU L4 (vLLM) | 1.000 decode / 8.000 prefill token/s | giả định — thay bằng load test |

| Kết quả | Giá trị |
|---|---|
| Số chunk ở 1 triệu tài liệu | **~47 triệu** |
| Index vector (HNSW) toàn bộ · mỗi phòng ban | **~84 GB · ~8,4 GB** |
| GPU cần ở mức trần 100 request | **10 GPU L4 → 4 máy g6.12xlarge** (dư 1 máy dự phòng) |
| Tải trần · tải trung bình dự kiến | ~11,8 · ~0,9 câu trả lời/giây (thấp hơn ~13 lần) |
| Index lần đầu · cập nhật hằng ngày | ~6,5 giờ trên 1 GPU · ~4 phút/ngày |

### Ba quyết định mà con số buộc phải có

1. **Chia bảng vector theo phòng ban.** Index ~84 GB không nằm gọn trong RAM của một máy database; ~8,4 GB mỗi phòng ban thì vừa. Điều này cũng trùng với mô hình cách ly: phòng ban nào đọc phân vùng của phòng ban đó.
2. **GPU scale theo độ dài hàng đợi, tối thiểu 2 máy.** "100 đồng thời" là mức trần, tải thường thấp hơn ~13 lần, nên không giữ cả cụm GPU chạy suốt ngày.
3. **Index là dữ liệu dẫn xuất.** Mất index thì dựng lại từ tài liệu trên S3 trong vài giờ; thời gian đó được tính vào ngân sách khôi phục (RTO).

### Sáu vấn đề đề bài yêu cầu

| Vấn đề | Hôm nay (đã có, đã test) | Khi mở rộng |
|---|---|---|
| Scale API, việc bất đồng bộ, backpressure | API stateless; giới hạn tần suất theo người gọi | ECS Fargate 3 AZ; **hàng đợi có giới hạn cho từng mô hình**, quá tải trả `429` (*đề xuất, chưa xây*); việc dài chạy qua SQS |
| Định tuyến mô hình, GPU, batching, fallback | Gateway có retry, fallback, circuit breaker; router chạy mô hình nhỏ riêng | vLLM batching liên tục; scale theo hàng đợi và KV-cache, không theo % GPU; API bên ngoài chỉ làm fallback cho dữ liệu public/internal |
| Embedding, index tăng dần, chia shard, vòng đời tài liệu | Bỏ qua tài liệu không đổi (hash); thay phiên bản nguyên khối; retry + dead-letter | Index lần đầu trên GPU spot; `halfvec`; chia theo phòng ban; đọc từ replica |
| Cache, hàng đợi, retry, dead-letter | — | Khóa cache **luôn chứa phạm vi quyền của người hỏi** (thiếu là rò rỉ chéo phòng ban); không cache dữ liệu confidential; SQS giữ nguyên cơ chế retry/DLQ |
| Cách ly phòng ban từ nạp → truy xuất → trích dẫn → audit | Phòng ban lấy từ quyền người upload; RLS + lọc SQL; confidential chỉ tới mô hình nội bộ; audit nối chuỗi hash | Thêm khóa KMS riêng từng phòng ban; audit xuất sang S3 Object Lock |
| Sẵn sàng, DR, quan sát, chi phí | Log JSON có correlation ID; metrics Prometheus; ghi usage từng lần gọi | 99,9% cho trả lời, 99,95% cho tool; RPO ≈ 5 phút, RTO ≈ 30 phút; trace OpenTelemetry (*đề xuất*); theo dõi tỉ lệ từ chối / thiếu trích dẫn / sửa nguồn như tín hiệu chất lượng; ngân sách token theo phòng ban |

### Khi quá tải — những điều **không bao giờ** xảy ra

- Không bỏ (shed) lệnh gọi tool hay phê duyệt — đó là thao tác có tác dụng phụ.
- Không gửi dữ liệu confidential ra API bên ngoài, kể cả khi fallback.
- Không trả lời mà không có nguồn — nếu mọi mô hình sinh câu đều lỗi, chỉ trả về đoạn trích có trích dẫn.
- Không thực hiện hành động nếu không ghi được audit.

### Nói thẳng

Đề xuất **chưa được load test**. Các đầu vào "giả định" là số để lập kế hoạch; việc đầu tiên khi triển khai thật là đo lại theo thứ tự: (1) recall và độ trễ pgvector trên một phân vùng ~8 GB, (2) thông lượng vLLM thật, (3) ngưỡng tải mà p95 bắt đầu tăng.

Chi tiết: `docs/decisions/D-60-scale-proposal-aws.md`.

---

## 7. Tài liệu tham khảo

| File | Nội dung |
|---|---|
| `README.md` | Tổng quan hệ thống, demo 6 hạng mục bắt buộc |
| `architecture.md` | Kiến trúc kỹ thuật chi tiết |
| `docs/decisions/` | 35 bản ghi quyết định |
| `docs/traceability.md` | Truy vết từng yêu cầu → code → test |
| `evaluation/capacity.py` | Mô hình tính công suất cho đề xuất mở rộng (`make capacity`) |
| `evaluation/reports/evaluation.md` | Báo cáo đánh giá sinh tự động |
| `evaluation/reports/analysis.md` | Phân tích tay từng ca lỗi |
