import { useEffect, useState } from "react";
import { Box, Typography, Alert, Paper, Stack, TextField, Button, Chip } from "@mui/material";
import { DataGrid } from "@mui/x-data-grid";
import DownloadIcon from "@mui/icons-material/Download";
import { getSku, exportUrl } from "../api/client.js";

const flagColor = { RT_RTO_AMBIGUITY: "error", SKU_RETURNED_GT_ORDERED: "error" };

const columns = [
  { field: "packet_id", headerName: "Packet ID", flex: 1, minWidth: 150 },
  { field: "sku_code", headerName: "SKU", flex: 1, minWidth: 130 },
  { field: "ordered_qty", headerName: "Ordered Qty", width: 120, type: "number" },
  { field: "rt_qty", headerName: "RT Qty", width: 100, type: "number" },
  { field: "rto_qty", headerName: "RTO Qty", width: 100, type: "number" },
  { field: "returned_qty", headerName: "Returned Qty", width: 130, type: "number" },
  { field: "flag", headerName: "Flag", flex: 1, minWidth: 200,
    renderCell: (p) => p.value
      ? <Chip size="small" label={p.value} color={flagColor[p.value] || "warning"} variant="outlined" />
      : null },
];

export default function SkuPage({ runId }) {
  const [rows, setRows] = useState([]);
  const [flag, setFlag] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);

  const load = () => {
    if (!runId) return;
    setLoading(true); setErr(null);
    const params = { limit: 5000 };
    if (flag) params.flag = flag;
    getSku(runId, params)
      .then(({ data }) => setRows(data.map((r) => ({ id: r.id, ...r }))))
      .catch((e) => setErr(e?.response?.data?.detail || e.message))
      .finally(() => setLoading(false));
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [runId]);

  if (!runId) return <Alert severity="info">No active run. Run a reconciliation first.</Alert>;

  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 2 }}>SKU &amp; Quantity — Run #{runId}</Typography>
      <Paper sx={{ p: 2, mb: 2 }}>
        <Stack direction="row" spacing={2} alignItems="center">
          <TextField label="Filter by flag (exact)" value={flag}
                     onChange={(e) => setFlag(e.target.value)}
                     onKeyDown={(e) => e.key === "Enter" && load()} sx={{ minWidth: 280 }} />
          <Button variant="outlined" onClick={load}>Apply</Button>
          <Box sx={{ flexGrow: 1 }} />
          <Button variant="contained" startIcon={<DownloadIcon />}
                  href={exportUrl(runId, "sku", "excel")}>Export Excel</Button>
          <Button variant="text" startIcon={<DownloadIcon />}
                  href={exportUrl(runId, "sku", "csv")}>CSV</Button>
        </Stack>
      </Paper>
      <Paper sx={{ height: 620 }}>
        <DataGrid
          rows={rows} columns={columns} loading={loading} density="compact"
          initialState={{ pagination: { paginationModel: { pageSize: 50 } } }}
          pageSizeOptions={[25, 50, 100]}
          disableRowSelectionOnClick
        />
      </Paper>
      {err && <Alert severity="error" sx={{ mt: 2 }}>{err}</Alert>}
    </Box>
  );
}
