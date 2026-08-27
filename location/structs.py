from struct import calcsize, unpack_from

# ported (trimmed to the position report only) from openpilot's
# system/qcomgpsd/structs.py -- see that file for the other DIAG log types
# (raw GPS/GLONASS measurement reports, OEMDRE reports) used by laikad,
# which this project doesn't need since it only wants a lat/lng fix, not
# raw pseudoranges.

LOG_GNSS_POSITION_REPORT = 0x1476

position_report = """
  uint8       u_Version;                /* Version number of DM log */
  uint32      q_Fcount;                 /* Local millisecond counter */
  uint8       u_PosSource;              /* Source of position information */ /*  0: None 1: Weighted least-squares 2: Kalman filter 3: Externally injected 4: Internal database    */
  uint32      q_Reserved1;              /* Reserved memory field */
  uint16      w_PosVelFlag;             /* Position velocity bit field: (see DM log 0x1476 documentation) */
  uint32      q_PosVelFlag2;            /* Position velocity 2 bit field: (see DM log 0x1476 documentation) */
  uint8       u_FailureCode;            /* Failure code: (see DM log 0x1476 documentation) */
  uint16      w_FixEvents;              /* Fix events bit field: (see DM log 0x1476 documentation) */
  uint32 _fake_align_week_number;
  uint16      w_GpsWeekNumber;          /* GPS week number of position */
  uint32      q_GpsFixTimeMs;           /* GPS fix time of week of in milliseconds */
  uint8       u_GloNumFourYear;         /* Number of Glonass four year cycles */
  uint16      w_GloNumDaysInFourYear;   /* Glonass calendar day in four year cycle */
  uint32      q_GloFixTimeMs;           /* Glonass fix time of day in milliseconds */
  uint32      q_PosCount;               /* Integer count of the number of unique positions reported */
  uint64      t_DblFinalPosLatLon[2];   /* Final latitude and longitude of position in radians */
  uint32      q_FltFinalPosAlt;         /* Final height-above-ellipsoid altitude of position */
  uint32      q_FltHeadingRad;          /* User heading in radians */
  uint32      q_FltHeadingUncRad;       /* User heading uncertainty in radians */
  uint32      q_FltVelEnuMps[3];        /* User velocity in east, north, up coordinate frame. In meters per second. */
  uint32      q_FltVelSigmaMps[3];      /* Gaussian 1-sigma value for east, north, up components of user velocity */
  uint32      q_FltClockBiasMeters;     /* Receiver clock bias in meters */
  uint32      q_FltClockBiasSigmaMeters;  /* Gaussian 1-sigma value for receiver clock bias in meters */
  uint32      q_FltGGTBMeters;          /* GPS to Glonass time bias in meters */
  uint32      q_FltGGTBSigmaMeters;     /* Gaussian 1-sigma value for GPS to Glonass time bias uncertainty in meters */
  uint32      q_FltGBTBMeters;          /* GPS to BeiDou time bias in meters */
  uint32      q_FltGBTBSigmaMeters;     /* Gaussian 1-sigma value for GPS to BeiDou time bias uncertainty in meters */
  uint32      q_FltBGTBMeters;          /* BeiDou to Glonass time bias in meters */
  uint32      q_FltBGTBSigmaMeters;     /* Gaussian 1-sigma value for BeiDou to Glonass time bias uncertainty in meters */
  uint32      q_FltFiltGGTBMeters;      /* Filtered GPS to Glonass time bias in meters */
  uint32      q_FltFiltGGTBSigmaMeters; /* Filtered Gaussian 1-sigma value for GPS to Glonass time bias uncertainty in meters */
  uint32      q_FltFiltGBTBMeters;      /* Filtered GPS to BeiDou time bias in meters */
  uint32      q_FltFiltGBTBSigmaMeters; /* Filtered Gaussian 1-sigma value for GPS to BeiDou time bias uncertainty in meters */
  uint32      q_FltFiltBGTBMeters;      /* Filtered BeiDou to Glonass time bias in meters */
  uint32      q_FltFiltBGTBSigmaMeters; /* Filtered Gaussian 1-sigma value for BeiDou to Glonass time bias uncertainty in meters */
  uint32      q_FltSftOffsetSec;        /* SFT offset as computed by WLS in seconds */
  uint32      q_FltSftOffsetSigmaSec;   /* Gaussian 1-sigma value for SFT offset in seconds */
  uint32      q_FltClockDriftMps;       /* Clock drift (clock frequency bias) in meters per second */
  uint32      q_FltClockDriftSigmaMps;  /* Gaussian 1-sigma value for clock drift in meters per second */
  uint32      q_FltFilteredAlt;         /* Filtered height-above-ellipsoid altitude in meters as computed by WLS */
  uint32      q_FltFilteredAltSigma;    /* Gaussian 1-sigma value for filtered height-above-ellipsoid altitude in meters */
  uint32      q_FltRawAlt;              /* Raw height-above-ellipsoid altitude in meters as computed by WLS */
  uint32      q_FltRawAltSigma;         /* Gaussian 1-sigma value for raw height-above-ellipsoid altitude in meters */
  uint32   align_Flt[14];
  uint32      q_FltPdop;                /* 3D position dilution of precision as computed from the unweighted */
  uint32      q_FltHdop;                /* Horizontal position dilution of precision as computed from the unweighted least-squares covariance matrix */
  uint32      q_FltVdop;                /* Vertical position dilution of precision as computed from the unweighted least-squares covariance matrix */
""" # noqa: E501


def name_to_camelcase(nam):
  ret = []
  i = 0
  while i < len(nam):
    if nam[i] == "_":
      ret.append(nam[i + 1].upper())
      i += 2
    else:
      ret.append(nam[i])
      i += 1
  return ''.join(ret)


def parse_struct(ss):
  st = "<"
  nams = []
  for l in ss.strip().split("\n"):
    if len(l.strip()) == 0:
      continue
    typ, nam = l.split(";")[0].split()
    if typ == "float" or '_Flt' in nam:
      st += "f"
    elif typ == "double" or '_Dbl' in nam:
      st += "d"
    elif typ in ["uint8", "uint8_t"]:
      st += "B"
    elif typ in ["int8", "int8_t"]:
      st += "b"
    elif typ in ["uint32", "uint32_t"]:
      st += "I"
    elif typ in ["int32", "int32_t"]:
      st += "i"
    elif typ in ["uint16", "uint16_t"]:
      st += "H"
    elif typ in ["int16", "int16_t"]:
      st += "h"
    elif typ in ["uint64", "uint64_t"]:
      st += "Q"
    else:
      raise RuntimeError(f"unknown type {typ}")
    if '[' in nam:
      cnt = int(nam.split("[")[1].split("]")[0])
      st += st[-1] * (cnt - 1)
      for i in range(cnt):
        nams.append(f'{nam.split("[")[0]}[{i}]')
    else:
      nams.append(nam)
  return st, nams


def dict_unpacker(ss, camelcase=False):
  st, nams = parse_struct(ss)
  if camelcase:
    nams = [name_to_camelcase(x) for x in nams]
  sz = calcsize(st)
  return lambda x: dict(zip(nams, unpack_from(st, x), strict=True)), sz
