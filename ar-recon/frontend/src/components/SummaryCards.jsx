import { Grid, Paper, Typography, Box } from "@mui/material";
import { KPMG } from "../theme.js";

const money = (n) =>
  (n ?? 0).toLocaleString("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
const num = (n) => (n ?? 0).toLocaleString("en-IN");

function Card({ label, value, accent, sub }) {
  return (
    <Paper sx={{ p: 2, height: "100%", borderTop: `3px solid ${accent}` }}>
      <Typography variant="caption" color="text.secondary" sx={{ textTransform: "uppercase", letterSpacing: 0.5 }}>
        {label}
      </Typography>
      <Typography variant="h5" sx={{ fontWeight: 700, mt: 0.5, color: KPMG.ink }}>
        {value}
      </Typography>
      {sub && <Typography variant="caption" color="text.secondary">{sub}</Typography>}
    </Paper>
  );
}

export default function SummaryCards({ cards }) {
  if (!cards) return null;
  const items = [
    { label: "Total Forward Sales", value: money(cards.total_forward_sales), accent: KPMG.blue },
    { label: "Total RT", value: money(cards.total_rt), accent: KPMG.mediumBlue },
    { label: "Total RTO", value: money(cards.total_rto), accent: KPMG.lightBlue },
    { label: "Net Sales", value: money(cards.net_sales), accent: KPMG.cobalt },
    { label: "Forward Settlement", value: money(cards.forward_settlement), accent: KPMG.blue },
    { label: "Reverse Settlement", value: money(cards.reverse_settlement), accent: KPMG.mediumBlue },
    { label: "Net Settlement", value: money(cards.net_settlement), accent: KPMG.lightBlue },
    { label: "Difference", value: money(cards.difference), accent: KPMG.teal },
    { label: "Matched Records", value: num(cards.matched_records), accent: KPMG.green },
    { label: "Unmatched Records", value: num(cards.unmatched_records), accent: KPMG.amber },
    { label: "Exception Records", value: num(cards.exception_records), accent: KPMG.magenta },
  ];
  return (
    <Grid container spacing={2}>
      {items.map((it) => (
        <Grid item xs={12} sm={6} md={3} key={it.label}>
          <Card {...it} />
        </Grid>
      ))}
    </Grid>
  );
}
