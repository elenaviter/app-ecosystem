/**
 * Where an info mark's bubble opens so that all of it stays inside the
 * viewport (W360, operator's review 2026-09-26: `review.assign`'s bubble opened
 * past the right edge of the Card screen).
 *
 * The bubble opens under its mark, starting at the mark's left edge. When
 * that would cross the viewport's right edge it moves left, never past the
 * left margin; when there is no room below and there is room above, it opens
 * above the mark instead.
 */

export const BUBBLE_MARGIN = 8;
export const BUBBLE_GAP = 6;

export interface AnchorBox {
  left: number;
  top: number;
  bottom: number;
}

export interface BubbleSize {
  width: number;
  height: number;
}

export interface ViewportSize {
  width: number;
  height: number;
}

export interface BubblePlacement {
  /** The bubble's left edge relative to the mark's left edge, in pixels. */
  left: number;
  above: boolean;
}

export function bubblePlacement(
  anchor: AnchorBox,
  bubble: BubbleSize,
  viewport: ViewportSize,
  margin: number = BUBBLE_MARGIN,
): BubblePlacement {
  const rightLimit = viewport.width - margin;
  let start = anchor.left;
  if (start + bubble.width > rightLimit) start = rightLimit - bubble.width;
  if (start < margin) start = margin;
  const roomBelow = viewport.height - margin - (anchor.bottom + BUBBLE_GAP);
  const roomAbove = anchor.top - BUBBLE_GAP - margin;
  const above = bubble.height > roomBelow && bubble.height <= roomAbove;
  return { left: Math.round(start - anchor.left), above };
}
