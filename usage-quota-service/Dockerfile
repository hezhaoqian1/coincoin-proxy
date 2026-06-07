FROM golang:1.23-bookworm AS build
WORKDIR /src

COPY go.mod go.sum ./
RUN go mod download

COPY . ./
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/usage-quota-service ./cmd/usage-quota-service

FROM gcr.io/distroless/static-debian12:nonroot
WORKDIR /app
COPY --from=build /out/usage-quota-service /app/usage-quota-service

EXPOSE 8091
ENTRYPOINT ["/app/usage-quota-service"]
