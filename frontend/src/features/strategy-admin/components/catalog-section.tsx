import { Icons } from '@/components/icons';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow
} from '@/components/ui/table';
import { cn } from '@/lib/utils';
import Link from 'next/link';
import { getSection } from '../catalog';
import type { CatalogBlock, CatalogSection, CatalogTone } from '../catalog';
import {
  Meter,
  StatTile,
  ToneBadge,
  toneGradient,
  toneSurface,
  toneText,
  toneTopBorder
} from './visuals';

function blockIcon(title: string): keyof typeof Icons {
  const value = title.toLowerCase();
  if (/gate|eligib|risk|cap|guard/.test(value)) return 'shield';
  if (/factor|ranking|score|distribution/.test(value)) return 'chart';
  if (/flag|penalt/.test(value)) return 'warning';
  if (/cost|simulation|execution|inventory|correlation/.test(value)) return 'coins';
  if (/regime/.test(value)) return 'activity';
  if (/ladder|range|rebalanc/.test(value)) return 'grid';
  if (/walk-forward|validation|fold|parameter|backtest|consistency/.test(value)) return 'flask';
  if (/paper/.test(value)) return 'page';
  return 'code';
}

function cellTone(column: string, cell: string): CatalogTone | null {
  const lower = column.toLowerCase();
  if (lower === 'points') return 'bad';
  if (lower === 'severity') {
    if (cell === 'critical') return 'bad';
    if (cell === 'warning') return 'warn';
    if (cell === 'info') return 'info';
  }
  if (/disabled|off\b|fails/i.test(cell)) return 'bad';
  if (/required|> 0%|enabled/i.test(cell)) return 'good';
  if (/warning|flag >/i.test(cell)) return 'warn';
  return null;
}

function CatalogBlockView({ block, accent }: { block: CatalogBlock; accent: CatalogTone }) {
  const Icon = Icons[blockIcon(block.title)];
  const className = block.fullWidth ? 'xl:col-span-2' : undefined;

  return (
    <Card className={className}>
      <CardHeader>
        <div className='flex items-start gap-3'>
          <div
            className={cn(
              'flex size-8 shrink-0 items-center justify-center rounded-lg border',
              toneSurface[accent]
            )}
          >
            <Icon className={cn('size-4', toneText[accent])} />
          </div>
          <div className='min-w-0 space-y-0.5'>
            <CardTitle className='text-base'>{block.title}</CardTitle>
            <CardDescription>{block.description}</CardDescription>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {block.kind === 'table' ? (
          <div className='overflow-x-auto'>
            <Table>
              <TableHeader>
                <TableRow>
                  {block.columns.map((column) => (
                    <TableHead
                      key={column}
                      className={cn(
                        column.toLowerCase() === 'points' && 'text-right',
                        column.toLowerCase() === 'weight' && 'w-32'
                      )}
                    >
                      {column}
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {block.rows.map((row) => (
                  <TableRow key={row[0]}>
                    {row.map((cell, index) => {
                      const column = block.columns[index] ?? '';
                      const lower = column.toLowerCase();
                      if (lower === 'severity') {
                        const tone: CatalogTone =
                          cell === 'critical' ? 'bad' : cell === 'warning' ? 'warn' : 'info';
                        return (
                          <TableCell key={`${row[0]}-${index}`} className='align-top'>
                            <ToneBadge tone={tone}>{cell}</ToneBadge>
                          </TableCell>
                        );
                      }
                      if (lower === 'weight') {
                        const percent = Number(cell.replace('%', ''));
                        return (
                          <TableCell key={`${row[0]}-${index}`} className='align-top'>
                            <div className='flex items-center gap-2'>
                              <Meter
                                value={Number.isFinite(percent) ? percent : 0}
                                max={20}
                                tone='violet'
                                className='w-20'
                              />
                              <span className='text-xs font-medium tabular-nums'>{cell}</span>
                            </div>
                          </TableCell>
                        );
                      }
                      const tone = index === 0 ? null : cellTone(column, cell);
                      return (
                        <TableCell
                          key={`${row[0]}-${index}`}
                          className={cn(
                            'align-top text-sm',
                            index === 0 && 'font-medium',
                            index > 0 && !tone && 'text-muted-foreground',
                            tone && toneText[tone],
                            lower === 'points' && 'text-right font-medium tabular-nums'
                          )}
                        >
                          {cell}
                        </TableCell>
                      );
                    })}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        ) : (
          <div className={cn('grid gap-2', block.fullWidth && 'md:grid-cols-2')}>
            {block.rows.map((row) => {
              const tone = row.tone ?? 'neutral';
              return (
                <div
                  key={row.name}
                  className={cn(
                    'rounded-lg border p-3',
                    row.tone ? toneSurface[row.tone] : 'bg-card'
                  )}
                >
                  <div className='flex items-start justify-between gap-3'>
                    <p className='text-sm font-medium'>{row.name}</p>
                    {row.value ? <ToneBadge tone={tone}>{row.value}</ToneBadge> : null}
                  </div>
                  <p className='text-muted-foreground mt-1 text-xs leading-relaxed'>
                    {row.detail}
                  </p>
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default function CatalogSectionPage({ sectionId }: { sectionId: CatalogSection['id'] }) {
  const section = getSection(sectionId);
  const Icon = Icons[section.icon];

  return (
    <div className='space-y-5'>
      <Link
        href='/admin/strategies'
        className='text-muted-foreground hover:text-foreground inline-flex items-center gap-1 text-xs font-medium'
      >
        <Icons.chevronLeft className='size-3.5' />
        All strategy sections
      </Link>

      <div className={cn('rounded-2xl border p-5', toneGradient[section.accent])}>
        <div className='flex items-start gap-4'>
          <div
            className={cn(
              'hidden size-12 shrink-0 items-center justify-center rounded-xl border sm:flex',
              toneSurface[section.accent]
            )}
          >
            <Icon className={cn('size-6', toneText[section.accent])} />
          </div>
          <div className='min-w-0 space-y-1'>
            <div className='flex flex-wrap items-center gap-2'>
              <h2 className='text-xl font-semibold tracking-tight'>{section.title}</h2>
              <ToneBadge tone={section.accent}>Section reference</ToneBadge>
            </div>
            <p className='text-muted-foreground max-w-4xl text-sm leading-relaxed'>
              {section.description}
            </p>
          </div>
        </div>
        <div className='mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-4'>
          {section.stats.map((stat) => (
            <StatTile key={stat.label} {...stat} />
          ))}
        </div>
      </div>

      <div className='grid gap-4 xl:grid-cols-2'>
        {section.blocks.map((block) => (
          <CatalogBlockView key={block.title} block={block} accent={section.accent} />
        ))}
      </div>

      <div
        className={cn(
          'rounded-xl border p-4',
          toneSurface[section.accent],
          toneTopBorder[section.accent]
        )}
      >
        <p className={cn('text-sm font-medium', toneText[section.accent])}>
          {section.blocks.length} reference blocks
        </p>
        <p className='text-muted-foreground mt-1 text-xs leading-relaxed'>
          Values are drawn from the engine configuration and scoring code. Thresholds shown here
          are defaults; the coin settings page reflects the values persisted by the latest analysis
          run.
        </p>
      </div>
    </div>
  );
}
