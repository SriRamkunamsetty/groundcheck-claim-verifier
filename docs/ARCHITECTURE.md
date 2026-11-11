# GroundCheck Enterprise Architecture & Design Specification

GroundCheck is a cloud-native, claim-level verification and evaluation platform for AI-written analytics. It prevents "existence fallacy" hallucinations by ensuring that every cited figure is real **and** attached to the exact correct metric and timeframe.

---

## 1. System Architecture Diagram

```mermaid
flowchart TD
    subgraph Client & Consumer Layer
        WEB_UI["Vanilla JS Claim Bench UI"]
        CLI_TOOL["GroundCheck CLI"]
        CLIENT_APPS["Upstream AI / Analytics Pipelines"]
        PROMETHEUS["Prometheus Monitoring"]
    end

    subgraph Kubernetes Cluster (groundcheck-prod)
        INGRESS["Nginx Ingress Controller (TLS Termination)"]

        subgraph Service Mesh / NetworkPolicy
            SVC["Kubernetes ClusterIP Service (:8000)"]

            subgraph Pod Replicas (HPA Managed: 2-10 Pods)
                FASTAPI["FastAPI Async Engine"]
                TRACING["Tracing Middleware (X-Request-ID)"]
                METRICS_COLLECTOR["Prometheus Metrics Collector (/metrics)"]

                subgraph AIML Verification Subsystem
                    SEMANTIC_ATTRIBUTOR["SemanticClaimAttributor (n-gram + Cosine)"]
                    NARRATOR_CORE["Deterministic Clause Verifier (1% RelTol)"]
                    ML_ANOMALY["MLAnomalyDetector (MAD + Isolation Forest)"]
                    STRUCTURED_JUDGE["StructuredJudge (Pydantic v2)"]
                end
            end

            POSTGRES[("PostgreSQL 16 HA Cluster")]
            REDIS[("Redis 7 Cache / Task Queue")]
        end
    end

    CLIENT_APPS -->|POST /api/v1/verify| INGRESS
    WEB_UI -->|HTTP| INGRESS
    CLI_TOOL -->|CLI API| FASTAPI
    PROMETHEUS -->|GET /metrics| INGRESS

    INGRESS --> SVC
    SVC --> FASTAPI
    FASTAPI --> TRACING
    TRACING --> NARRATOR_CORE
    NARRATOR_CORE --> SEMANTIC_ATTRIBUTOR
    NARRATOR_CORE --> ML_ANOMALY
    FASTAPI --> STRUCTURED_JUDGE
    FASTAPI --> METRICS_COLLECTOR

    FASTAPI -->|Read / Write| POSTGRES
    FASTAPI -->|Cache / Sessions| REDIS
```

---

## 2. Core Architectural Pillars

### 2.1 The Semantic Claim Attribution Engine
Standard LLM fact-checkers fail when presented with **swapped-window** errors:
> *"The campaign drew 1.99M views in the first 72 hours."*
> If `velocity_24h = 1.99M` and `velocity_72h = 3.48M`, an existence check passes.

GroundCheck enforces **Clause-Level Attribution**:
1. **Clause Segmentation**: Parses sentences into atomic clauses based on boundary markers: `;, (), ., !?`.
2. **Entity Masking**: Strips registered entities with numbers (e.g. *"Apollo 13"*, *"Series 2"*) so they are not flagged as orphan claims.
3. **Numeric Extraction & Normalization**:
   - Converts `K`, `M`, `B` multipliers to absolute floats ($1.99\text{M} \rightarrow 1,990,000$).
   - Normalizes percentages and bare ratios ($8.49\% \rightarrow 0.0849$).
   - Skips bare years (1900–2100) and window units (`72 hours`, `7-day`).
4. **Hybrid Attribution**:
   - **Deterministic Regex Cues**: Immediate match with confidence $1.0$.
   - **Semantic n-gram Cosine Similarity**: Evaluates candidate metric synonyms using vectorized character n-grams ($n \in [3, 4]$) and word tokens:
     $$\text{Cosine}(A, B) = \frac{A \cdot B}{\|A\| \|B\|}$$
   - If a clause semantically aligns with metric $M_1$, but the cited number equals metric $M_2$, GroundCheck flags a **`misattributed`** violation.
5. **Numerical Tolerance**:
   Validates claim $x$ against candidate fact $c$ within a strict **1% relative tolerance**:
   $$|x - c| \le 0.01 \cdot |c| + 10^{-9}$$

---

### 2.2 Machine Learning Anomaly Detection
To prevent models from hallucinating false spikes or missing real market shifts, `MLAnomalyDetector` executes:
1. **Robust-Z Scoring via Median Absolute Deviation (MAD)** on day-over-day log growth:
   $$\text{MAD} = \text{median}(|x_i - \text{median}(x)|)$$
   $$z = 0.6745 \cdot \frac{x - \text{median}}{\max(\text{MAD}, 0.05)}$$
2. **Isolation Forest Verification**:
   When `scikit-learn` is installed, multi-dimensional rate distributions are passed to an Isolation Forest to score anomaly severity without parametric normality assumptions.
3. **Event Temporal Window Correlation**:
   Correlates detected spikes against logged marketing events within $\pm 48\text{h}$ to provide automated explanatory context.

---

### 2.3 Production Kubernetes & DevOps Topology

* **Multi-Stage Distroless/Slim Container**:
  Built with unprivileged `appuser:10001`, `readOnlyRootFilesystem: true`, and `capabilities.drop: ["ALL"]`.
* **Zero-Downtime Rolling Updates**:
  `maxSurge: 25%`, `maxUnavailable: 0` paired with `PodDisruptionBudget (minAvailable: 1)`.
* **Horizontal Pod Autoscaling (HPA)**:
  Autoscales between 2 and 10 pod replicas based on CPU ($75\%$) and Memory ($80\%$) utilization.
* **Observability**:
  Native `/metrics` endpoint exporting Prometheus metrics:
  - `groundcheck_claims_verified_total`
  - `groundcheck_misattributions_total`
  - `groundcheck_unsupported_total`
  - `groundcheck_style_violations_total`
  - `groundcheck_http_request_duration_seconds`

---

## 3. Threat Model & Security Posture

| Threat | Mitigation in GroundCheck |
|---|---|
| **Prompt Injection / Jailbreak** | Pure algorithmic verifier core does not execute arbitrary LLM prompts for verification; LLM judge uses strict JSON schema isolation. |
| **XSS / HTML Injection** | Web dashboard strictly avoids `innerHTML`, using `document.createElement` and `textContent`. Content-Security-Policy disallows external scripts. |
| **Denial of Service (DoS)** | FastAPI payload size capped at 10,000 characters; HTTP request timeouts; Kubernetes resource quotas and HPA. |
| **Privilege Escalation** | Kubernetes `securityContext` enforces `allowPrivilegeEscalation: false`, non-root execution, and read-only root filesystem. |
