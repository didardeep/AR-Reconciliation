import { Paper, Typography, Box, Table, TableBody, TableCell, TableHead, TableRow } from "@mui/material";
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip, CartesianGrid, Cell,
} from "recharts";
import { CHART_COLORS, KPMG } from "../theme.js";

export default function ExceptionSummary({ data }) {
  const rows = data || [];
  return (
    <Paper sx={{ p: 2 }}>
      <Typography variant="h6" sx={{ mb: 1 }}>Exception Summary</Typography>
      {rows.length === 0 ? (
        <Typography color="text.secondary" variant="body2">No exceptions for this run.</Typography>
      ) : (
        <Box sx={{ display: "flex", flexWrap: "wrap", gap: 3 }}>
          <Box sx={{ flex: "1 1 380px", height: 320 }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={rows} margin={{ top: 8, right: 16, left: 0, bottom: 60 }}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="exception_type" angle={-35} textAnchor="end" interval={0} height={70}
                       tick={{ fontSize: 11 }} />
                <YAxis allowDecimals={false} tick={{ fontSize: 12 }} />
                <Tooltip />
                <Bar dataKey="count" radius={[4, 4, 0, 0]}>
                  {rows.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </Box>
          <Box sx={{ flex: "1 1 320px" }}>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell sx={{ fontWeight: 700, color: KPMG.blue }}>Exception Type</TableCell>
                  <TableCell align="right" sx={{ fontWeight: 700, color: KPMG.blue }}>Count</TableCell>
                  <TableCell align="right" sx={{ fontWeight: 700, color: KPMG.blue }}>%</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {rows.map((r) => (
                  <TableRow key={r.exception_type} hover>
                    <TableCell>{r.exception_type}</TableCell>
                    <TableCell align="right">{r.count}</TableCell>
                    <TableCell align="right">{r.percentage}%</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Box>
        </Box>
      )}
    </Paper>
  );
}
