import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Paper, Typography, Box, Button, MenuItem, TextField, Alert, Grid, CircularProgress,
} from "@mui/material";
import { listFiles, runReconciliation } from "../api/client.js";

const byType = (files, t) => files.filter((f) => f.file_type === t);

export default function RunPage({ onRun }) {
  const nav = useNavigate();
  const [files, setFiles] = useState([]);
  const [form, setForm] = useState({
    reconciliation_month: "2024-02",
    tolerance: 1.0,
    sales_forward_file_id: "",
    sales_rt_file_id: "",
    sales_rto_file_id: "",
    settlement_forward_file_id: "",
    settlement_reverse_file_id: "",
    link_file_id: "",
  });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  useEffect(() => { listFiles().then(({ data }) => setFiles(data)).catch(() => {}); }, []);
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  const picker = (label, key, type, required) => (
    <Grid item xs={12} md={6}>
      <TextField
        select fullWidth required={required} label={label}
        value={form[key]} onChange={set(key)}
      >
        <MenuItem value=""><em>— none —</em></MenuItem>
        {byType(files, type).map((f) => (
          <MenuItem key={f.id} value={f.id}>#{f.id} · {f.filename}</MenuItem>
        ))}
      </TextField>
    </Grid>
  );

  const onSubmit = async () => {
    if (!form.sales_forward_file_id || !form.settlement_forward_file_id) {
      return setMsg({ type: "warning", text: "Sales Forward and Settlement Forward are required." });
    }
    setBusy(true); setMsg(null);
    const payload = {
      reconciliation_month: form.reconciliation_month,
      tolerance: Number(form.tolerance),
      sales_forward_file_id: Number(form.sales_forward_file_id),
      settlement_forward_file_id: Number(form.settlement_forward_file_id),
      sales_rt_file_id: form.sales_rt_file_id ? Number(form.sales_rt_file_id) : null,
      sales_rto_file_id: form.sales_rto_file_id ? Number(form.sales_rto_file_id) : null,
      settlement_reverse_file_id: form.settlement_reverse_file_id ? Number(form.settlement_reverse_file_id) : null,
      link_file_id: form.link_file_id ? Number(form.link_file_id) : null,
    };
    try {
      const { data } = await runReconciliation(payload);
      onRun(String(data.id));
      setMsg({ type: "success", text: `Run #${data.id} completed — ${data.total_matched} matched, ${data.total_exceptions} exceptions.` });
      setTimeout(() => nav("/dashboard"), 700);
    } catch (e) {
      setMsg({ type: "error", text: `Run failed: ${e?.response?.data?.detail || e.message}` });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 2 }}>Run Reconciliation</Typography>
      <Paper sx={{ p: 3 }}>
        <Grid container spacing={2}>
          <Grid item xs={12} md={6}>
            <TextField fullWidth label="Reconciliation month (YYYY-MM)"
                       value={form.reconciliation_month} onChange={set("reconciliation_month")} />
          </Grid>
          <Grid item xs={12} md={6}>
            <TextField fullWidth type="number" label="Tolerance"
                       value={form.tolerance} onChange={set("tolerance")}
                       inputProps={{ step: "0.01", min: 0 }} />
          </Grid>
          {picker("Sales — Forward *", "sales_forward_file_id", "sales_forward", true)}
          {picker("Settlement — Forward *", "settlement_forward_file_id", "settlement_forward", true)}
          {picker("Sales — RT", "sales_rt_file_id", "sales_rt")}
          {picker("Sales — RTO", "sales_rto_file_id", "sales_rto")}
          {picker("Settlement — Reverse", "settlement_reverse_file_id", "settlement_reverse")}
          {picker("Sales↔Settlement Link", "link_file_id", "sales_settlement_link")}
        </Grid>
        <Box sx={{ mt: 3 }}>
          <Button variant="contained" size="large" onClick={onSubmit} disabled={busy}>
            {busy ? <CircularProgress size={22} color="inherit" /> : "Run reconciliation"}
          </Button>
        </Box>
        {msg && <Alert severity={msg.type} sx={{ mt: 2 }}>{msg.text}</Alert>}
      </Paper>
    </Box>
  );
}
