import { Fragment, type ReactNode } from "react";
import { Linking, Text, View } from "react-native";
import { alpha, mono, radius, text, useTheme } from "@/theme";

/**
 * The subset of Markdown Lumen's replies use: paragraphs, headings, bullet and numbered lists, code
 * blocks, inline code, bold, italic, and links. Links open only when tapped.
 */
type Block =
  | { kind: "paragraph"; text: string }
  | { kind: "heading"; level: number; text: string }
  | { kind: "list"; ordered: boolean; items: string[] }
  | { kind: "code"; text: string };

function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  let paragraph: string[] = [];
  const flush = () => {
    if (paragraph.length) blocks.push({ kind: "paragraph", text: paragraph.join("\n") });
    paragraph = [];
  };
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (/^\s*```/.test(line)) {
      flush();
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !/^\s*```/.test(lines[index])) {
        code.push(lines[index]);
        index += 1;
      }
      blocks.push({ kind: "code", text: code.join("\n") });
      continue;
    }
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      flush();
      blocks.push({ kind: "heading", level: heading[1].length, text: heading[2] });
      continue;
    }
    const bullet = /^\s*(?:[-*•]|(\d+)[.)])\s+(.*)$/.exec(line);
    if (bullet) {
      flush();
      const ordered = Boolean(bullet[1]);
      const last = blocks[blocks.length - 1];
      if (last?.kind === "list" && last.ordered === ordered) last.items.push(bullet[2]);
      else blocks.push({ kind: "list", ordered, items: [bullet[2]] });
      continue;
    }
    if (!line.trim()) {
      flush();
      continue;
    }
    paragraph.push(line);
  }
  flush();
  return blocks;
}

const INLINE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\[[^\]]+\]\((?:https?:\/\/)[^)\s]+\))|(https?:\/\/[^\s)]+)|(\*[^*\s][^*]*\*)|(_[^_\s][^_]*_)/g;

function Inline({ value, color }: { value: string; color: string }) {
  const { c } = useTheme();
  const parts: ReactNode[] = [];
  let last = 0;
  let match: RegExpExecArray | null;
  INLINE.lastIndex = 0;
  while ((match = INLINE.exec(value))) {
    if (match.index > last) parts.push(value.slice(last, match.index));
    const token = match[0];
    const key = `${match.index}-${token.length}`;
    if (match[1]) {
      parts.push(
        <Text key={key} style={{ fontFamily: mono, fontSize: 13, backgroundColor: alpha(c.fg, 0.07), color: c.fg }}>
          {token.slice(1, -1)}
        </Text>
      );
    } else if (match[2]) {
      parts.push(
        <Text key={key} style={{ fontWeight: "700", color: c.fg }}>
          {token.slice(2, -2)}
        </Text>
      );
    } else if (match[3] || match[4]) {
      const link = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(token);
      const label = link ? link[1] : token;
      const url = link ? link[2] : token;
      parts.push(
        <Text key={key} style={{ color: c.brandInk, textDecorationLine: "underline" }} onPress={() => void Linking.openURL(url)}>
          {label}
        </Text>
      );
    } else {
      parts.push(
        <Text key={key} style={{ fontStyle: "italic" }}>
          {token.slice(1, -1)}
        </Text>
      );
    }
    last = match.index + token.length;
  }
  if (last < value.length) parts.push(value.slice(last));
  return <Text style={{ ...text.md, color }}>{parts.map((part, index) => (typeof part === "string" ? <Fragment key={index}>{part}</Fragment> : part))}</Text>;
}

export function Markdown({ content, color }: { content: string; color?: string }) {
  const { c } = useTheme();
  const ink = color ?? c.fg;
  const blocks = parseBlocks(content);
  return (
    <View style={{ gap: 10 }}>
      {blocks.map((block, index) => {
        if (block.kind === "heading") {
          return (
            <Text key={index} style={{ fontSize: block.level <= 2 ? 17 : 15, lineHeight: 22, fontWeight: "700", color: c.fg }}>
              {block.text}
            </Text>
          );
        }
        if (block.kind === "code") {
          return (
            <View key={index} style={{ backgroundColor: c.surface2, borderRadius: radius.md, borderWidth: 1, borderColor: c.line, padding: 10 }}>
              <Text selectable style={{ fontFamily: mono, fontSize: 12.5, lineHeight: 18, color: c.fg }}>
                {block.text}
              </Text>
            </View>
          );
        }
        if (block.kind === "list") {
          return (
            <View key={index} style={{ gap: 6 }}>
              {block.items.map((item, itemIndex) => (
                <View key={itemIndex} style={{ flexDirection: "row", gap: 8 }}>
                  <Text style={{ ...text.md, color: c.fgSubtle, minWidth: block.ordered ? 18 : 10 }}>{block.ordered ? `${itemIndex + 1}.` : "•"}</Text>
                  <View style={{ flex: 1 }}>
                    <Inline value={item} color={ink} />
                  </View>
                </View>
              ))}
            </View>
          );
        }
        return <Inline key={index} value={block.text} color={ink} />;
      })}
    </View>
  );
}
