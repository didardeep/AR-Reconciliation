import { useEffect, useState } from "react";
import { Box, Typography, Alert, Stack, CircularProgress } from "@mui/material";
import SummaryCards from "../components/SummaryCards.jsx";
import ExceptionSummary from "../components/ExceptionSummary.jsx";
import { getSummary, getRun } from "../api/client.js";

export default function DashboardPage({ runId }) {
  const [summary, setSummary] = useState(null);
  const [cards, setCards] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    if (!runId) return;
    setLoading(true); setErr(null);
    getSummary(runId)
      .then(({ data }) => {
        setSummary(data.exception_summary);
        setCards({
          ...(data.cards || {}),
          matched_records: data.matched,
          unmatched_records: data.unmatched,
        });
      })
      .catch((e) => setErr(e?.response?.data?.detail || e.message))
      .finally(() => setLoading(false));
  }, [runId]);

  if (!runId) return <Alert severity="info">No active run. Upload files and run a reconciliation first.</Alert>;

  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 2 }}>Dashboard — Run #{runId}</Typography>
      {loading && <Stack alignItems="center" sx={{ my: 4 }}><CircularProgress /></Stack>}
      {err && <Alert severity="error" sx={{ mb: 2 }}>{err}</Alert>}
      {cards && (
        <Box sx={{ mb: 3 }}>
          <SummaryCards cards={cards} />
        </Box>
      )}
      {summary && <ExceptionSummary data={summary} />}
    </Box>
  );
}
