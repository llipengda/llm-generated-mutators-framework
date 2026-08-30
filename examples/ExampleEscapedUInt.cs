using System;
using System.Collections.Generic;
using System.IO;
using System.Xml;
using Peach.Core;
using Peach.Core.Analyzers;
using Peach.Core.Cracker;
using Peach.Core.Dom;
using Peach.Core.IO;

[assembly: PluginAssembly]

namespace Peach.LLM.Examples.Dom
{
    // Demonstration-only wire type. Each byte contains six payload bits,
    // bit 6 is the continuation flag, and bit 7 is reserved and must be zero.
    // Encodings are little-endian base-64 groups, use one to three bytes, and
    // must be minimal. This is intentionally not a real protocol type.
    [DataElement("ExampleEscapedUInt", DataElementTypes.NonDataElements)]
    [PitParsable("ExampleEscapedUInt")]
    [Parameter("name", typeof(string), "Element name", "")]
    [Parameter("fieldId", typeof(string), "Element field ID", "")]
    [Parameter("value", typeof(string), "Default value", "")]
    [Parameter("valueType", typeof(Peach.Core.Dom.ValueType), "Value format", "string")]
    [Parameter("token", typeof(bool), "Is element a token", "false")]
    [Parameter("mutable", typeof(bool), "Is element mutable", "true")]
    [Parameter("constraint", typeof(string), "Value constraint", "")]
    [Parameter("minOccurs", typeof(int), "Minimum occurrences", "1")]
    [Parameter("maxOccurs", typeof(int), "Maximum occurrences", "1")]
    [Parameter("occurs", typeof(int), "Actual occurrences", "1")]
    [Serializable]
    public sealed class ExampleEscapedUInt : Number
    {
        private const ulong MaximumValue = 0x3ffffUL;

        public ExampleEscapedUInt()
            : base()
        {
            Initialize();
        }

        public ExampleEscapedUInt(string name)
            : base(name)
        {
            Initialize();
        }

        private void Initialize()
        {
            lengthType = LengthType.Bits;
            base.length = 18;
            Signed = false;
            LittleEndian = false;
            DefaultValue = new Variant(0UL);
        }

        public override bool hasLength
        {
            get { return false; }
        }

        public override bool isDeterministic
        {
            get { return true; }
        }

        public override long length
        {
            get { return EncodedByteCount(ReadNumericValue()) * 8L; }
            set { base.length = 18; }
        }

        private static int EncodedByteCount(ulong value)
        {
            if (value <= 0x3fUL)
                return 1;
            if (value <= 0xfffUL)
                return 2;
            return 3;
        }

        private ulong ReadNumericValue()
        {
            if (InternalValue == null)
                return 0;

            switch (InternalValue.GetVariantType())
            {
                case Variant.VariantType.ULong:
                    return (ulong)InternalValue;
                case Variant.VariantType.Long:
                {
                    long value = (long)InternalValue;
                    if (value < 0)
                        throw new PeachException("ExampleEscapedUInt cannot be negative.");
                    return (ulong)value;
                }
                case Variant.VariantType.Int:
                {
                    int value = (int)InternalValue;
                    if (value < 0)
                        throw new PeachException("ExampleEscapedUInt cannot be negative.");
                    return (ulong)value;
                }
                default:
                    throw new PeachException("ExampleEscapedUInt requires an integer value.");
            }
        }

        protected override BitwiseStream InternalValueToBitStream()
        {
            ulong value = ReadNumericValue();
            if (value > MaximumValue)
                throw new PeachException("ExampleEscapedUInt exceeds its 18-bit range.");

            var stream = new BitStream();
            do
            {
                byte encoded = (byte)(value & 0x3fUL);
                value >>= 6;
                if (value != 0)
                    encoded |= 0x40;
                stream.WriteByte(encoded);
            }
            while (value != 0);

            stream.Seek(0, SeekOrigin.Begin);
            return stream;
        }

        protected override Variant Sanitize(Variant variant)
        {
            ulong value;
            switch (variant.GetVariantType())
            {
                case Variant.VariantType.String:
                {
                    string text = (string)variant;
                    value = text.StartsWith("0x", StringComparison.OrdinalIgnoreCase)
                        ? Convert.ToUInt64(text.Substring(2), 16)
                        : Convert.ToUInt64(text);
                    break;
                }
                case Variant.VariantType.ULong:
                    value = (ulong)variant;
                    break;
                case Variant.VariantType.Long:
                {
                    long signed = (long)variant;
                    if (signed < 0)
                        throw new PeachException("ExampleEscapedUInt cannot be negative.");
                    value = (ulong)signed;
                    break;
                }
                case Variant.VariantType.Int:
                {
                    int signed = (int)variant;
                    if (signed < 0)
                        throw new PeachException("ExampleEscapedUInt cannot be negative.");
                    value = (ulong)signed;
                    break;
                }
                case Variant.VariantType.ByteString:
                    value = Decode(new BitStream((byte[])variant));
                    break;
                case Variant.VariantType.BitStream:
                {
                    var stream = (BitwiseStream)variant;
                    long position = stream.PositionBits;
                    try
                    {
                        value = Decode(stream);
                    }
                    finally
                    {
                        stream.PositionBits = position;
                    }
                    break;
                }
                default:
                    throw new PeachException("Unsupported ExampleEscapedUInt value type.");
            }

            if (value > MaximumValue)
                throw new PeachException("ExampleEscapedUInt exceeds its 18-bit range.");
            return new Variant(value);
        }

        private static ulong Decode(BitwiseStream stream)
        {
            ulong value = 0;
            for (int index = 0; index < 3; ++index)
            {
                if (stream.PositionBits + 8 > stream.LengthBits)
                    throw new PeachException("Truncated ExampleEscapedUInt encoding.");

                byte encoded = (byte)stream.ReadByte();
                if ((encoded & 0x80) != 0)
                    throw new PeachException("Reserved bit is set in ExampleEscapedUInt.");

                ulong payload = (ulong)(encoded & 0x3f);
                value |= payload << (index * 6);
                bool continued = (encoded & 0x40) != 0;
                if (!continued)
                {
                    if (index > 0 && payload == 0)
                        throw new PeachException("Non-minimal ExampleEscapedUInt encoding.");
                    return value;
                }
            }
            throw new PeachException("ExampleEscapedUInt exceeds three bytes.");
        }

        public override void Crack(DataCracker context, BitStream data, long? size)
        {
            var bytes = new List<byte>();
            for (int index = 0; index < 3; ++index)
            {
                if (data.PositionBits + 8 > data.LengthBits)
                    throw new CrackingFailure(
                        "Truncated ExampleEscapedUInt encoding.", this, data);

                byte encoded = (byte)data.ReadByte();
                bytes.Add(encoded);
                if ((encoded & 0x80) != 0)
                    throw new CrackingFailure(
                        "Reserved bit is set in ExampleEscapedUInt.", this, data);
                if ((encoded & 0x40) == 0)
                    break;
            }

            if ((bytes[bytes.Count - 1] & 0x40) != 0)
                throw new CrackingFailure(
                    "ExampleEscapedUInt exceeds three bytes.", this, data);

            ulong value;
            try
            {
                value = Decode(new BitStream(bytes.ToArray()));
            }
            catch (PeachException error)
            {
                throw new CrackingFailure(error.Message, this, data);
            }

            DefaultValue = new Variant(value);
            if (context.IsLogEnabled)
                context.Log("Value: {0}, encoded bytes: {1}", value, bytes.Count);
        }

        public new static DataElement PitParser(
            PitParser context,
            XmlNode node,
            DataElementContainer parent)
        {
            if (node.Name != "ExampleEscapedUInt")
                return null;

            var element = DataElement.Generate<ExampleEscapedUInt>(node, parent);
            context.handleCommonDataElementAttributes(node, element);
            context.handleCommonDataElementChildren(node, element);
            context.handleCommonDataElementValue(node, element);
            return element;
        }

        public override void WritePit(XmlWriter pit)
        {
            pit.WriteStartElement("ExampleEscapedUInt");
            WritePitCommonAttributes(pit);
            WritePitCommonChildren(pit);
            WritePitCommonValue(pit);
            pit.WriteEndElement();
        }
    }
}
