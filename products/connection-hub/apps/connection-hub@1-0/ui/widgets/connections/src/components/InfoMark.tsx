/**
 * A small information mark that explains the control beside it in one
 * sentence, on hover and on focus. Help lives here, never as a paragraph
 * above a form: the form stays bare and the explanation is one gesture away.
 */
export function InfoMark({ text }: { text: string }) {
  return (
    <span className="info-mark" tabIndex={0} role="img" aria-label={text} title={text}>
      i
    </span>
  );
}
