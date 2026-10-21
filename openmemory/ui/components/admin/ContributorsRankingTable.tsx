"use client";

import Link from "next/link";
import { ActorLabel } from "@/components/shared/attribution-badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { ContributorMetric, TopContributor } from "@/types/admin";

interface ContributorsRankingTableProps {
  items: TopContributor[];
  metric: ContributorMetric;
}

export function ContributorsRankingTable({ items, metric }: ContributorsRankingTableProps) {
  const isScore = metric === "total";

  if (items.length === 0) {
    return null;
  }

  return (
    <div className="rounded-xl border border-zinc-800">
      <div className="border-b border-zinc-800 px-4 py-3">
        <h2 className="text-base font-semibold text-white">Detalhamento do ranking</h2>
      </div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-16">#</TableHead>
            <TableHead>Usuário</TableHead>
            <TableHead>Grupo</TableHead>
            <TableHead>Escritas</TableHead>
            <TableHead>Consultas</TableHead>
            <TableHead>{isScore ? "Score" : "Total"}</TableHead>
            <TableHead>Projetos</TableHead>
            <TableHead className="text-right">Ações</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {items.map((item) => (
            <TableRow key={item.user_id}>
              <TableCell className="font-semibold text-zinc-300">{item.rank}</TableCell>
              <TableCell>
                <ActorLabel
                  hostname={item.user_id}
                  displayName={item.display_name}
                  avatarUrl={item.avatar_url}
                />
              </TableCell>
              <TableCell className="text-zinc-400">{item.group_name ?? "—"}</TableCell>
              <TableCell>{item.writes}</TableCell>
              <TableCell>{item.reads}</TableCell>
              <TableCell className="font-medium text-zinc-100">
                {isScore ? item.value.toFixed(1) : item.value}
              </TableCell>
              <TableCell>{item.distinct_projects}</TableCell>
              <TableCell className="text-right">
                {item.group_id ? (
                  <Link
                    href={`/admin/users/${item.group_id}/${encodeURIComponent(item.user_id)}`}
                    className="rounded-md bg-zinc-800 px-3 py-1.5 text-sm text-zinc-100 hover:bg-zinc-700"
                  >
                    Detalhes
                  </Link>
                ) : (
                  <span className="text-sm text-zinc-600">—</span>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

export default ContributorsRankingTable;
