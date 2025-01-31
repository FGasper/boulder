package web

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	blog "github.com/letsencrypt/boulder/log"
)

type myHandler struct{}

func (m myHandler) ServeHTTP(e *RequestEvent, w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(201)
	e.Endpoint = "/endpoint"
	_, _ = w.Write([]byte("hi"))
}

func TestLogCode(t *testing.T) {
	mockLog := blog.UseMock()
	th := NewTopHandler(mockLog, myHandler{})
	req, err := http.NewRequest("GET", "/thisisignored", &bytes.Reader{})
	if err != nil {
		t.Fatal(err)
	}
	th.ServeHTTP(httptest.NewRecorder(), req)
	expected := `INFO: GET /endpoint 0 201 0 0.0.0.0 JSON={}`
	if 1 != len(mockLog.GetAllMatching(expected)) {
		t.Errorf("Expected exactly one log line matching %q. Got \n%s",
			expected, strings.Join(mockLog.GetAllMatching(".*"), "\n"))
	}
}

type codeHandler struct{}

func (ch codeHandler) ServeHTTP(e *RequestEvent, w http.ResponseWriter, r *http.Request) {
	e.Endpoint = "/endpoint"
	_, _ = w.Write([]byte("hi"))
}

func TestStatusCodeLogging(t *testing.T) {
	mockLog := blog.UseMock()
	th := NewTopHandler(mockLog, codeHandler{})
	req, err := http.NewRequest("GET", "/thisisignored", &bytes.Reader{})
	if err != nil {
		t.Fatal(err)
	}
	th.ServeHTTP(httptest.NewRecorder(), req)
	expected := `INFO: GET /endpoint 0 200 0 0.0.0.0 JSON={}`
	if 1 != len(mockLog.GetAllMatching(expected)) {
		t.Errorf("Expected exactly one log line matching %q. Got \n%s",
			expected, strings.Join(mockLog.GetAllMatching(".*"), "\n"))
	}
}

func TestOrigin(t *testing.T) {
	mockLog := blog.UseMock()
	th := NewTopHandler(mockLog, myHandler{})
	req, err := http.NewRequest("GET", "/thisisignored", &bytes.Reader{})
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Add("Origin", "https://example.com")
	th.ServeHTTP(httptest.NewRecorder(), req)
	expected := `INFO: GET /endpoint 0 201 0 0.0.0.0 JSON={.*"Origin":"https://example.com"}`
	if 1 != len(mockLog.GetAllMatching(expected)) {
		t.Errorf("Expected exactly one log line matching %q. Got \n%s",
			expected, strings.Join(mockLog.GetAllMatching(".*"), "\n"))
	}
}
