import { useEffect, useState } from "react";
import { Box, Typography, Alert, Paper, Stack, TextField, Button, Chip } from "@mui/material";
import { DataGrid } from "@mui/x-data-grid";
import DownloadIcon from "@mui/icons-material/Download";
import { getExceptions, exportUrl } from "../api/client.js";

const sevColor = { ERROR: "error", WARNING: "warning", INFO: "info" };

const columns = [
  { field: "code", headerName: "Code", flex: 1, minWidth: 190 },
  { field: "severity", headerName: "Severity", width: 120,
    renderCell: (p) => <Chip size="small" label={p.value} color={sevColor[p.value] || "default"} variant="outlined" /> },
  { field: "stage", headerName: "Stage", width: 110 },
  { field: "record_id", headerName: "Record ID", flex: 1, minWidth: 150 },
  { field: "source_file", headerName: "Source", width: 150 },
  { field: "description", headerName: "Description", flex: 2, minWidth: 260 },
  { field: "difference", headerName: "Diff", width: 100, type: "number" },
];

export default function ExceptionsPage({ runId }) {
  const [rows, setRows] = useState([]);
  const [code, setCode] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);

  const load = () => {
    if (!runId) return;
    setLoading(true); setErr(null);
    const params = { limit: 10000 };
    if (code) params.code = code;
    getExceptions(runId, params)
      .then(({ data }) => setRows(data.map((r) => ({ id: r.id, ...r }))))
      .catch((e) => setErr(e?.response?.data?.detail || e.message))
      .finally(() => setLoading(false));
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [runId]);

  if (!runId) return <Alert severity="info">No active run. Run a reconciliation first.</Alert>;

  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 2 }}>Exceptions — Run #{runId}</Typography>
      <Paper sx={{ p: 2, mb: 2 }}>
        <Stack direction="row" spacing={2} alignItems="center">
          <TextField label="Filter by code (exact)" value={code}
                     onChange={(e) => setCode(e.target.value)}
                     onKeyDown={(e) => e.key === "Enter" && load()} sx={{ minWidth: 280 }} />
          <Button variant="outlined" onClick={load}>Apply</Button>
          <Box sx={{ flexGrow: 1 }} />
          <Button variant="contained" startIcon={<DownloadIcon />}
                  href={exportUrl(runId, "exceptions", "excel")}>Export Excel</Button>
          <Button variant="text" startIcon={<DownloadIcon />}
                  href={exportUrl(runId, "exceptions", "csv")}>CSV</Button>
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
