import { Chip } from "@mui/material";

export default function StatusChip({ value }) {
  const matched = value === "MATCHED";
  return (
    <Chip
      size="small"
      label={value}
      color={matched ? "success" : "error"}
      variant={matched ? "filled" : "outlined"}
      sx={{ fontWeight: 600 }}
    />
  );
}
