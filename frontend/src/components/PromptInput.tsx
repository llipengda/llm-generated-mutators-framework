interface Props {
  label: string;
  hint?: string;
  placeholder?: string;
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}

export function PromptInput({ label, hint, placeholder, value, onChange, disabled }: Props) {
  return (
    <div className="mb-5">
      <label className="block text-xs text-muted font-medium mb-1">
        {label}
      </label>
      {hint && (
        <p className="text-xs text-muted mb-2 leading-relaxed">{hint}</p>
      )}
      <textarea
        className="w-full px-3 py-2 border border-border rounded-sm bg-white text-[13px] leading-relaxed resize-y min-h-[96px] font-sans
          focus:outline-none focus:border-primary
          disabled:opacity-50 disabled:cursor-not-allowed
          placeholder:text-muted"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        disabled={disabled}
        rows={4}
      />
    </div>
  );
}
