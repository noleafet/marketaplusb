import type { ReactNode } from 'react';

function inline(text: string, keyPrefix: string): ReactNode[] {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean);
  return parts.map((part, index) => {
    const key = `${keyPrefix}-${index}`;
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
      return <strong key={key}>{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return (
        <code key={key} className='bg-muted rounded px-1 py-0.5 font-mono text-xs'>
          {part.slice(1, -1)}
        </code>
      );
    }
    return <span key={key}>{part}</span>;
  });
}

function splitRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim());
}

export default function MarkdownReport({ markdown }: { markdown: string }) {
  const lines = markdown.replace(/\r\n/g, '\n').split('\n');
  const blocks: ReactNode[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    const trimmed = line.trim();

    if (!trimmed) {
      index += 1;
      continue;
    }

    if (trimmed.startsWith('```')) {
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith('```')) {
        code.push(lines[index]);
        index += 1;
      }
      index += 1;
      blocks.push(
        <pre
          key={`code-${index}`}
          className='bg-muted overflow-x-auto rounded-lg p-3 font-mono text-xs'
        >
          {code.join('\n')}
        </pre>
      );
      continue;
    }

    const heading = /^(#{1,4})\s+(.*)$/.exec(trimmed);
    if (heading) {
      const level = heading[1].length;
      const content = inline(heading[2], `h-${index}`);
      const className =
        level === 1
          ? 'text-2xl font-semibold tracking-tight'
          : level === 2
            ? 'text-xl font-semibold'
            : 'text-base font-semibold';
      blocks.push(
        <div key={`h-${index}`} className={className}>
          {content}
        </div>
      );
      index += 1;
      continue;
    }

    if (trimmed.startsWith('|')) {
      const rows: string[] = [];
      while (index < lines.length && lines[index].trim().startsWith('|')) {
        rows.push(lines[index]);
        index += 1;
      }
      if (rows.length >= 2) {
        const header = splitRow(rows[0]);
        const body = rows.slice(2).map(splitRow);
        blocks.push(
          <div key={`table-${index}`} className='overflow-x-auto'>
            <table className='w-full text-sm'>
              <thead>
                <tr className='border-border border-b'>
                  {header.map((cell, cellIndex) => (
                    <th key={cellIndex} className='px-2 py-2 text-left font-medium'>
                      {inline(cell, `th-${index}-${cellIndex}`)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {body.map((row, rowIndex) => (
                  <tr key={rowIndex} className='border-border/60 border-b last:border-0'>
                    {row.map((cell, cellIndex) => (
                      <td key={cellIndex} className='px-2 py-1.5 tabular-nums'>
                        {inline(cell, `td-${index}-${rowIndex}-${cellIndex}`)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      }
      continue;
    }

    if (/^[-*]\s+/.test(trimmed)) {
      const items: string[] = [];
      while (index < lines.length && /^[-*]\s+/.test(lines[index].trim())) {
        items.push(lines[index].trim().replace(/^[-*]\s+/, ''));
        index += 1;
      }
      blocks.push(
        <ul key={`ul-${index}`} className='list-disc space-y-1 pl-5 text-sm'>
          {items.map((item, itemIndex) => (
            <li key={itemIndex}>{inline(item, `li-${index}-${itemIndex}`)}</li>
          ))}
        </ul>
      );
      continue;
    }

    if (/^\d+\.\s+/.test(trimmed)) {
      const items: string[] = [];
      while (index < lines.length && /^\d+\.\s+/.test(lines[index].trim())) {
        items.push(lines[index].trim().replace(/^\d+\.\s+/, ''));
        index += 1;
      }
      blocks.push(
        <ol key={`ol-${index}`} className='list-decimal space-y-1 pl-5 text-sm'>
          {items.map((item, itemIndex) => (
            <li key={itemIndex}>{inline(item, `oli-${index}-${itemIndex}`)}</li>
          ))}
        </ol>
      );
      continue;
    }

    const paragraph: string[] = [];
    while (index < lines.length) {
      const candidate = lines[index].trim();
      if (
        !candidate ||
        candidate.startsWith('#') ||
        candidate.startsWith('|') ||
        candidate.startsWith('```') ||
        /^[-*]\s+/.test(candidate) ||
        /^\d+\.\s+/.test(candidate)
      ) {
        break;
      }
      paragraph.push(candidate);
      index += 1;
    }
    blocks.push(
      <p key={`p-${index}`} className='text-sm leading-relaxed'>
        {inline(paragraph.join(' '), `p-${index}`)}
      </p>
    );
  }

  return <div className='space-y-3'>{blocks}</div>;
}
