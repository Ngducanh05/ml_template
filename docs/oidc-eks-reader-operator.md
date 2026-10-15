# GitHub OIDC → EKS Release State v2 Reader

Reader path của Release v2 không dùng `RELEASE_STATE_READER_B64`. Mỗi workflow
run phải lấy GitHub OIDC, assume đúng IAM role Kubernetes Reader, kiểm tra danh
tính STS và metadata của đúng EKS cluster, tạo EKS token trong bộ nhớ rồi GET
đúng `ConfigMap/production/model-release-state-v2`.

Token Kubernetes không được ghi vào GitHub Secret, log hoặc artifact. File tạm
chỉ chứa snapshot Release State không có credential, được tạo với quyền `0600`
trong `${RUNNER_TEMP}` và bị xóa bằng step `if: always()`.

## Cấu hình owner cần xác nhận

Reader ARN không nhạy cảm được khóa trực tiếp trong hai workflow:
`arn:aws:iam::596432515999:role/ml-template-v2-k8s-reader`. Không cần tạo GitHub
Secret hoặc Variable mới cho Reader. Giữ nguyên biến
`RELEASE_CATALOG_READ_ROLE_ARN`; không dùng IAM Kubernetes Reader cho ECR và
không mở rộng Catalog Reader sang `eks:DescribeCluster`.

Không cần tạo lại `RELEASE_STATE_READER_B64`. Sau khi rollout hoàn tất và mọi
Reader workflow đã chuyển sang OIDC, secret cũ có thể được owner xóa trong một
thay đổi settings riêng có evidence. Writer `RELEASE_STATE_WRITER_B64` vẫn giữ
nguyên trong Environment `production`.

## Kiểm tra không cần Kubernetes network

Mở **Actions → OIDC EKS Reader Probe → Run workflow**, giữ `live_get=false`.
Job GitHub-hosted phải in đủ:

```text
OIDC_K8S_READER_IDENTITY_PASS
OIDC_EKS_DESCRIBE_CLUSTER_PASS
OIDC_K8S_TOKEN_GENERATION_PASS
```

Đây chỉ chứng minh STS, `eks:DescribeCluster` và token generation. Nó không phải
bằng chứng Kubernetes API hoặc RBAC PASS.

## Kubernetes Live GET trên runner có network hợp lệ

Chỉ thực hiện sau khi runner `mlops-release-readonly` đã được nghiệm thu và có
đường mạng tới EKS public endpoint trong CIDR allowlist hiện tại. Không mở rộng
endpoint thành `0.0.0.0/0`.

1. Xử lý các workflow run cũ đang queued trước khi đưa runner online.
2. Mở **Actions → OIDC EKS Reader Probe → Run workflow** trên `main`.
3. Chọn `live_get=true`.
4. Xác minh job `live-get` chạy trên đúng Linux/X64 runner có label
   `mlops-release-readonly`.

PASS chỉ khi log có đủ bốn marker và không có token:

```text
OIDC_K8S_READER_IDENTITY_PASS
OIDC_EKS_DESCRIBE_CLUSTER_PASS
OIDC_K8S_TOKEN_GENERATION_PASS
OIDC_KUBERNETES_API_GET_PASS
```

Nếu job dừng sau token generation, kiểm tra firewall/CIDR, DNS và TLS tới EKS
API từ chính runner. Nếu API trả `401`, kiểm tra Access Entry IAM role. Nếu trả
`403`, kiểm tra group `mlops-v2-state-reader-oidc`, Role và RoleBinding. Không
fallback sang kubeconfig admin hoặc credential khác.

## Điều kiện kích hoạt Reader workflows

- Probe token-generation PASS.
- Live GET PASS trên runner thật.
- Reader ARN khóa trong workflow khác role do `RELEASE_CATALOG_READ_ROLE_ARN`
  cung cấp.
- Runner boundary đã được duyệt.
- Stale queued workflows đã được xử lý.

Scheduled runtime trigger không được bật lại chỉ dựa trên source tests. Release
preflight không phải Production PASS; Writer, Security Gate, Human Approval,
CAS và Canary vẫn là các gate độc lập.
