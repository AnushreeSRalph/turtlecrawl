/**
 * turtlecrawl k6 load test script
 *
 * Environment variables (set via --env or the agent's run_load_test tool):
 *   BASE_URL  — e.g. http://sample-app.turtlecrawl.svc:8080
 *   VUS       — virtual users (concurrent)
 *   DURATION  — e.g. "60s", "2m"
 *
 * Usage:
 *   k6 run --env BASE_URL=http://localhost:8080 --env VUS=50 --env DURATION=60s loadtest/script.js
 */

import http from "k6/http";
import { check, sleep } from "k6";
import { Rate, Trend } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8080";
const VUS = parseInt(__ENV.VUS || "50");
const DURATION = __ENV.DURATION || "60s";

// Custom metrics
const errorRate = new Rate("error_rate");
const slowRequests = new Rate("slow_requests"); // requests > 100ms

export const options = {
  vus: VUS,
  duration: DURATION,
  thresholds: {
    // SLO targets — these will FAIL the test if breached
    http_req_duration: ["p(99)<120"],   // p99 < 120ms
    error_rate: ["rate<0.01"],          // < 1% errors
    http_req_failed: ["rate<0.01"],
  },
};

export default function () {
  // Mix of fast and slow requests (80/20 split — realistic traffic pattern)
  const r = Math.random();

  let res;
  if (r < 0.8) {
    // Fast path — typical API call
    res = http.get(`${BASE_URL}/`, {
      tags: { endpoint: "index" },
      timeout: "5s",
    });
  } else if (r < 0.95) {
    // Slow path — simulates DB-bound request
    res = http.get(`${BASE_URL}/slow?delay=0.05`, {
      tags: { endpoint: "slow" },
      timeout: "5s",
    });
  } else {
    // CPU-bound path
    res = http.get(`${BASE_URL}/stress?ms=30`, {
      tags: { endpoint: "stress" },
      timeout: "5s",
    });
  }

  // Track custom metrics
  const isError = res.status >= 400;
  const isSlow = res.timings.duration > 100;

  errorRate.add(isError);
  slowRequests.add(isSlow);

  check(res, {
    "status is 2xx": (r) => r.status >= 200 && r.status < 300,
    "response time < 200ms": (r) => r.timings.duration < 200,
  });

  // Think time — simulate realistic user pacing
  sleep(Math.random() * 0.5 + 0.1); // 0.1–0.6s between requests
}

export function handleSummary(data) {
  // Output a clean JSON summary the agent can parse
  return {
    stdout: JSON.stringify({
      p99_latency_ms: data.metrics.http_req_duration?.values?.["p(99)"] || 0,
      p95_latency_ms: data.metrics.http_req_duration?.values?.["p(95)"] || 0,
      avg_latency_ms: data.metrics.http_req_duration?.values?.avg || 0,
      rps: data.metrics.http_reqs?.values?.rate || 0,
      error_rate: data.metrics.http_req_failed?.values?.rate || 0,
      total_requests: data.metrics.http_reqs?.values?.count || 0,
      vus: VUS,
      duration: DURATION,
      thresholds_passed: Object.entries(data.metrics).every(([_, m]) =>
        !m.thresholds || Object.values(m.thresholds).every((t) => !t.ok === false)
      ),
    }, null, 2),
  };
}
