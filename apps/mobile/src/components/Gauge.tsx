/**
 * 270-degree gauge for a headline score (confidence, hit rate, agreement).
 * Clay arc on a hairline track, numeral in the middle. Colours come from the
 * palette tokens, never raw hex.
 */

import { useColorScheme } from 'nativewind';
import Svg, { Circle, Text as SvgText } from 'react-native-svg';

import { palette } from '@app/ui';

import { gaugeGeometry, showsGaugeLabel } from './gaugeMath';

export function Gauge({
  value,
  size = 84,
  label,
  suffix = '',
}: {
  /** 0-100. */
  value: number;
  size?: number;
  label?: string;
  suffix?: string;
}) {
  const { colorScheme } = useColorScheme();
  const c = palette[colorScheme === 'light' ? 'light' : 'dark'];
  const { stroke, r, circumference, sweep, fill } = gaugeGeometry(value, size);
  const half = size / 2;
  const withLabel = showsGaugeLabel(label, size);
  const rotate = `rotate(135 ${half} ${half})`;
  const valueText = `${Math.round(value)}${suffix}`;

  return (
    <Svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      accessible
      accessibilityRole="image"
      accessibilityLabel={`${Math.round(value)}${suffix}${label ? ` ${label}` : ''}`}
    >
      <Circle
        cx={half}
        cy={half}
        r={r}
        fill="none"
        stroke={c.hairline}
        strokeWidth={stroke}
        strokeLinecap="round"
        strokeDasharray={`${sweep} ${circumference}`}
        transform={rotate}
      />
      <Circle
        cx={half}
        cy={half}
        r={r}
        fill="none"
        stroke={c.accentPrimary}
        strokeWidth={stroke}
        strokeLinecap="round"
        strokeDasharray={`${fill} ${circumference}`}
        transform={rotate}
      />
      <SvgText
        x={half}
        y={withLabel ? half - size * 0.04 : half + 1}
        textAnchor="middle"
        alignmentBaseline="central"
        fill={c.textPrimary}
        fontSize={Math.round(size * (withLabel ? 0.31 : 0.36))}
        fontWeight="500"
      >
        {valueText}
      </SvgText>
      {withLabel ? (
        <SvgText
          x={half}
          y={half + size * 0.24}
          textAnchor="middle"
          alignmentBaseline="central"
          fill={c.textTertiary}
          fontSize={12}
          fontWeight="500"
        >
          {label}
        </SvgText>
      ) : null}
    </Svg>
  );
}
