FROM golang:1.26.9-alpine3.24@sha256:397ecc648150dd585f1734dfcd8d1bb4746664e54464191d77ad0ecffaf38bdb AS build
WORKDIR /src
ADD --checksum=sha256:d629314a4b8eaa64ef7e72ff87e0e1d2f62825b33e0cdfa37f642bc174d670f7 https://codeload.github.com/kubernetes/kube-state-metrics/tar.gz/refs/tags/v2.20.0 /tmp/source.tar.gz
RUN tar -xzf /tmp/source.tar.gz --strip-components=1 -C /src
RUN go get golang.org/x/crypto@v0.55.0 google.golang.org/grpc@v1.83.2 && go mod tidy
RUN CGO_ENABLED=0 go test ./internal/store/... ./pkg/options/...
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w -X github.com/prometheus/common/version.Version=2.20.0-patched" -o /out/kube-state-metrics .
FROM gcr.io/distroless/static-debian13:nonroot@sha256:2293b36c7c9082bf4115aab724b4d2cddec82c8eba39bf27ac0517e159acf150
COPY --from=build /out/kube-state-metrics /kube-state-metrics
USER 65532:65532
ENTRYPOINT ["/kube-state-metrics"]
