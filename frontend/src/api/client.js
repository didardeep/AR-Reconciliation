import axios from "axios";

const baseURL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

export const api = axios.create({ baseURL, timeout: 1800000 }); // 30 min for large files

// ---- Files ----
export const uploadFile = (file, fileType) => {
  const form = new FormData();
  form.append("file", file);
  form.append("file_type", fileType);
  return api.post("/api/files/upload", form, {
    headers: { "Content-Type": "multipart/form-data" },
  });
};
export const listFiles = () => api.get("/api/files");

// ---- Reconciliation ----
export const runReconciliation = (payload) => api.post("/api/reconciliation/run", payload);
export const getRun = (id) => api.get(`/api/reconciliation/${id}`);
export const getSummary = (id) => api.get(`/api/reconciliation/${id}/summary`);
export const getResults = (id, params) => api.get(`/api/reconciliation/${id}/results`, { params });
export const getExceptions = (id, params) => api.get(`/api/reconciliation/${id}/exceptions`, { params });
export const getSales = (id, params) => api.get(`/api/reconciliation/${id}/sales`, { params });
export const getSettlement = (id, params) => api.get(`/api/reconciliation/${id}/settlement`, { params });
export const getSku = (id, params) => api.get(`/api/reconciliation/${id}/sku`, { params });

export const exportUrl = (id, kind, fmt) =>
  `${baseURL}/api/reconciliation/${id}/export?kind=${kind}&fmt=${fmt}`;

export const FILE_TYPES = [
  { value: "sales_forward", label: "Sales — Forward" },
  { value: "sales_rt", label: "Sales — RT" },
  { value: "sales_rto", label: "Sales — RTO" },
  { value: "settlement_forward", label: "Settlement — Forward" },
  { value: "settlement_reverse", label: "Settlement — Reverse" },
  { value: "sales_settlement_link", label: "Sales↔Settlement Link (optional)" },
];
