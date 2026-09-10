import { useEffect, useState } from "react";
import {
  Box, Typography, Alert, Paper, Stack, TextField, MenuItem, Button,
} from "@mui/material";
import { DataGrid } from "@mui/x-data-grid";
import DownloadIcon from "@mui/icons-material/Download";
import StatusChip from "../components/StatusChip.jsx";
import { getResults, exportUrl } from "../api/client.js";

const STATUS = ["", "MATCHED", "EXCEPTION"];
const EXC = ["", "AMOUNT_MISMATCH", "SALES_ONLY", "SETTLEMENT_ONLY", "MISSING_MAPPING"];

const money = (v) => (v == null ? "" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 }));

const columns = [
  { field: "packet_id", headerName: "Packet ID", flex: 1, minWidth: 150 },
  { field: "order_release_id", headerName: "Order Release ID", flex: 1, minWidth: 150 },
  { field: "net_sales", headerName: "Net Sales", width: 130, type: "number", valueFormatter: (v) => money(v) },
  { field: "net_settlement", headerName: "Net Settlement", width: 140, type: "number", valueFormatter: (v) => money(v) },
  { field: "difference", headerName: "Difference", width: 120, type: "number", valueFormatter: (v) => money(v) },
  { field: "status", headerName: "Status", width: 140, renderCell: (p) => <StatusChip value={p.value} /> },
  { field: "exception_type", headerName: "Exception", flex: 1, minWidth: 160 },
];

export default function ResultsPage({ runId }) {
  const [rows, setRows] = useState([]);
  const [status, setStatus] = useState("");
  const [exc, setExc] = useState("");
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);

  const load = () => {
    if (!runId) return;
    setLoading(true); setErr(null);
    const params = {};
    if (status) params.status = status;
    if (exc) params.exception_type = exc;
    if (q) params.q = q;
    params.limit = 5000;
    getResults(runId, params)
      .then(({ data }) => setRows(data.map((r) => ({ id: r.id, ...r }))))
      .catch((e) => setErr(e?.response?.data?.detail || e.message))
      .finally(() => setLoading(false));
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [runId, status, exc]);

  if (!runId) return <Alert severity="info">No active run. Run a reconciliation first.</Alert>;

  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 2 }}>Reconciliation Results — Run #{runId}</Typography>
      <Paper sx={{ p: 2, mb: 2 }}>
        <Stack direction={{ xs: "column", md: "row" }} spacing={2} alignItems="center">
          <TextField select label="Status" value={status} onChange={(e) => setStatus(e.target.value)} sx={{ minWidth: 160 }}>
            {STATUS.map((s) => <MenuItem key={s} value={s}>{s || "All"}</MenuItem>)}
          </TextField>
          <TextField select label="Exception type" value={exc} onChange={(e) => setExc(e.target.value)} sx={{ minWidth: 200 }}>
            {EXC.map((s) => <MenuItem key={s} value={s}>{s || "All"}</MenuItem>)}
          </TextField>
          <TextField label="Search packet / order id" value={q}
                     onChange={(e) => setQ(e.target.value)}
                     onKeyDown={(e) => e.key === "Enter" && load()} sx={{ minWidth: 240 }} />
          <Button variant="outlined" onClick={load}>Search</Button>
          <Box sx={{ flexGrow: 1 }} />
          <Button variant="contained" startIcon={<DownloadIcon />}
                  href={exportUrl(runId, "final", "excel")}>Export Excel</Button>
          <Button variant="text" startIcon={<DownloadIcon />}
                  href={exportUrl(runId, "final", "csv")}>CSV</Button>
        </Stack>
      </Paper>
      <Paper sx={{ height: 620 }}>
        <DataGrid
          rows={rows} columns={columns} loading={loading}
          density="compact"
          initialState={{ pagination: { paginationModel: { pageSize: 50 } } }}
          pageSizeOptions={[25, 50, 100]}
          disableRowSelectionOnClick
        />
      </Paper>
      {err && <Alert severity="error" sx={{ mt: 2 }}>{err}</Alert>}
    </Box>
  );
}
