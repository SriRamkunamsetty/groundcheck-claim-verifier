# Kubernetes & Helm Production Deployment Guide

This guide covers deploying GroundCheck to Kubernetes clusters (EKS, GKE, AKS, or local Minikube/Kind) using either Helm or raw Kustomize manifests.

---

## 1. Prerequisites
* Kubernetes cluster v1.26+
* `kubectl` configured with cluster admin context
* Helm v3.12+ (if deploying via Helm)

---

## 2. Option A: Deploy via Helm (Recommended)

### 2.1 Install the Helm Chart
```bash
# Navigate to the chart directory
cd deploy/helm/groundcheck

# Install or upgrade the release
helm upgrade --install groundcheck . \
  --namespace groundcheck-prod \
  --create-namespace \
  --set env.ENVIRONMENT=production \
  --set autoscaling.enabled=true \
  --set autoscaling.minReplicas=2 \
  --set autoscaling.maxReplicas=10
```

### 2.2 Verify Pods & Services
```bash
kubectl get pods -n groundcheck-prod -o wide
kubectl get svc -n groundcheck-prod
kubectl get hpa -n groundcheck-prod
```

### 2.3 Check Liveness & Readiness Probes
```bash
# Verify endpoints respond
kubectl port-forward svc/groundcheck 8000:8000 -n groundcheck-prod

# In another terminal:
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/ready
curl http://127.0.0.1:8000/metrics
```

---

## 3. Option B: Deploy via Kustomize / Raw Manifests

```bash
# Apply namespace, configmap, deployment, service, HPA, and ingress
kubectl apply -k deploy/k8s/

# Monitor rollout status
kubectl rollout status deployment/groundcheck-api -n groundcheck-prod
```

---

## 4. Production Checklist & Hardening

1. **Horizontal Pod Autoscaling (HPA)**:
   The chart provisions an HPA scaling between 2 and 10 pods when CPU utilization exceeds 75% or memory exceeds 80%:
   ```bash
   kubectl describe hpa groundcheck -n groundcheck-prod
   ```

2. **Pod Disruption Budget (PDB)**:
   A PDB ensures at least 1 pod replica remains available during cluster node drains, maintenance, or rolling upgrades:
   ```bash
   kubectl get pdb -n groundcheck-prod
   ```

3. **Zero-Trust NetworkPolicy**:
   Default NetworkPolicy restricts inbound traffic strictly to port 8000 and restricts egress to internal Postgres (5432), DNS (53), and outbound HTTPS (443) for cloud LLM APIs.

4. **Prometheus Scraping**:
   Add this snippet to your Prometheus scrape configs or verify that the ServiceMonitor discovers `prometheus.io/scrape: "true"` annotations:
   ```yaml
   - job_name: 'groundcheck'
     kubernetes_sd_configs:
       - role: pod
     relabel_configs:
       - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
         action: keep
         regex: true
   ```

---

## 5. Teardown & Uninstall
```bash
# Uninstall Helm release
helm uninstall groundcheck -n groundcheck-prod

# Or delete Kustomize resources
kubectl delete -k deploy/k8s/
```
